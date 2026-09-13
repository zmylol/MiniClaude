from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError
from yarl import URL

from mini_claude.core.tools.builtin.web_fetch import WebFetchTool, _proxy_for_url

_PUBLIC_IP = "93.184.216.34"
_ARTICLE = (
    "<html><head><title>Research notes</title></head><body><nav>Navigation</nav><article>"
    "<h1>Research notes</h1><p>These research notes explain how safe network access works "
    "and provide sufficient context to preserve the main article during extraction.</p>"
    '<p>Read the <a href="https://example.org/reference">reference document</a> for '
    "additional details and examples of network behavior.</p></article></body></html>"
)


# 提供公共 DNS 解析结果，阻止测试发起真实域名查询
@pytest.fixture(autouse=True)
def public_dns() -> Iterator[MagicMock]:
    with patch(
        "socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 443)),
        ],
    ) as resolver:
        yield resolver


# 模拟 aiohttp 会话并保留实际请求参数与分块数据，完全禁止外部网络
@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    # 为当前测试安装响应处理器并收集原始请求
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        class Session:
            # 返回模拟会话上下文
            async def __aenter__(self) -> Session:
                return self

            # 结束模拟会话上下文
            async def __aexit__(self, *args: object) -> None:
                pass

            # 记录请求参数并将模拟响应转换成 aiohttp 流式接口
            @asynccontextmanager
            async def get(
                self, url: URL, *, headers: dict[str, str], server_hostname: str, **kwargs: object
            ) -> AsyncIterator[object]:
                request = httpx.Request(
                    "GET",
                    str(url),
                    headers=headers,
                    extensions={"sni_hostname": server_hostname, **kwargs},
                )
                requests.append(request)
                response = handler(request)
                yield SimpleNamespace(
                    status=response.status_code,
                    headers=response.headers,
                    charset="utf-8",
                    content=SimpleNamespace(iter_chunked=lambda _: response.aiter_bytes()),
                )

        monkeypatch.setattr(
            "mini_claude.core.tools.builtin.web_fetch.aiohttp.ClientSession",
            lambda **kwargs: Session(),
        )
        return requests

    return install


# 功能：验证真实正文抽取保留标题与链接，连接固定到已校验 IP 并保留 TLS 主机名
# 设计：拦截传输层而不替换提取器，同时验证抽取效果及 DNS 重绑定防护的实际请求
async def test_fetch_extracts_article_and_pins_connection(transport: Callable[..., object]) -> None:
    requests = transport(
        lambda _: httpx.Response(200, text=_ARTICLE, headers={"content-type": "text/html"})
    )
    result = await WebFetchTool().invoke({"url": "https://example.com/article"})
    assert not result.is_error, result.content
    payload = json.loads(result.content)
    assert payload["title"] == "Research notes"
    assert "safe network access" in payload["content"]
    assert "https://example.org/reference" in payload["content"]
    assert payload["final_url"] == "https://example.com/article"
    assert payload["fetched_at"] and "untrusted" in payload["notice"].lower()
    request = requests[0]
    assert request.url.host == _PUBLIC_IP
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"


# 功能：验证相对链接以实际页面目录解析而不是错误地拼到网站根目录
# 设计：文章位于二级路径并包含同目录链接，使用真实提取器暴露上游默认链接解析缺陷
async def test_fetch_preserves_relative_links(transport: Callable[..., object]) -> None:
    body = _ARTICLE.replace("https://example.org/reference", "reference.html#details")
    transport(lambda _: httpx.Response(200, text=body, headers={"content-type": "text/html"}))
    result = await WebFetchTool().invoke({"url": "https://example.com/guides/article"})
    assert not result.is_error
    assert (
        "https://example.com/guides/reference.html#details" in json.loads(result.content)["content"]
    )


# 功能：验证纯文本分页可连续读取，末页正确标记结束
# 设计：使用唯一字符序列检查页边界和游标，避免只判断字符串长度
async def test_fetch_paginates_plaintext(transport: Callable[..., object]) -> None:
    content = "abcdefghij"
    transport(lambda _: httpx.Response(200, text=content, headers={"content-type": "text/plain"}))
    first = json.loads(
        (await WebFetchTool().invoke({"url": "https://example.com", "max_chars": 4})).content
    )
    last = json.loads(
        (
            await WebFetchTool().invoke({"url": "https://example.com", "start": 4, "max_chars": 10})
        ).content
    )
    assert first["content"] == "abcd" and first["next_start"] == 4 and first["truncated"]
    assert last["content"] == "efghij" and last["next_start"] is None and not last["truncated"]


# 功能：验证 JSON 转义后的整体结果仍保留完整来源、提示和下一页游标
# 设计：大量引号和长 URL 会放大序列化长度，用真实分页结果检查不会被历史压缩器截断
async def test_fetch_bounds_serialized_result(transport: Callable[..., object]) -> None:
    transport(
        lambda _: httpx.Response(200, text='"' * 7000, headers={"content-type": "text/plain"})
    )
    result = await WebFetchTool().invoke({"url": "https://example.com/" + "x" * 1800})
    assert not result.is_error
    assert len(result.content) <= 7800
    page = json.loads(result.content)
    assert page["next_start"] == len(page["content"])
    assert page["truncated"] and page["notice"] and page["fetched_at"]


# 功能：验证 VPN 假 IP 只对域名触发受信任 HTTPS DNS 查询，随后仍连接真实公共 IP
# 设计：模拟基准网段解析和 DNS JSON 响应，观察实际 DoH 目标、TLS 主机名与后续固定 IP
async def test_fetch_resolves_vpn_fake_ip(
    public_dns: MagicMock, transport: Callable[..., object]
) -> None:
    public_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.4", 443))]

    # 区分固定的 DoH 查询与目标页面请求
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "1.1.1.1":
            assert request.url.params["name"] == "example.com"
            assert request.extensions["allow_redirects"] is False
            return httpx.Response(
                200, json={"Status": 0, "Answer": [{"type": 1, "data": _PUBLIC_IP}]}
            )
        return httpx.Response(200, text="public text", headers={"content-type": "text/plain"})

    requests = transport(respond)
    result = await WebFetchTool().invoke({"url": "https://example.com"})
    assert not result.is_error, result.content
    assert [r.url.host for r in requests] == ["1.1.1.1", _PUBLIC_IP]
    assert requests[0].extensions["sni_hostname"] == "1.1.1.1"
    assert requests[1].extensions["sni_hostname"] == "example.com"


# 功能：验证 DoH 回答也必须全部为公网地址
# 设计：通过 VPN 假 IP 进入备用解析，再返回私有地址，断言目标页面从未被连接
async def test_fetch_rejects_private_doh_answer(
    public_dns: MagicMock, transport: Callable[..., object]
) -> None:
    public_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.4", 443))]
    requests = transport(
        lambda _: httpx.Response(
            200,
            json={
                "Status": 0,
                "Answer": [{"type": 1, "data": "127.0.0.1"}],
            },
        )
    )
    result = await WebFetchTool().invoke({"url": "https://example.com"})
    assert result.is_error and result.error_type == "permission_denied"
    assert len(requests) == 1 and requests[0].url.host == "1.1.1.1"


# 功能：验证用户直接指定基准网段 IP 时不会借用备用 DNS 绕过拒绝规则
# 设计：输入字面量地址，断言没有 DNS 请求或网页请求
async def test_fetch_rejects_literal_fake_ip(
    public_dns: MagicMock, transport: Callable[..., object]
) -> None:
    requests = transport(lambda _: httpx.Response(200, text="private"))
    result = await WebFetchTool().invoke({"url": "http://198.18.0.4/"})
    assert result.is_error and result.error_type == "permission_denied"
    assert not requests
    public_dns.assert_not_called()


# 功能：验证外部页面不能重定向到内网、凭据地址或非 HTTP 资源
# 设计：枚举常见 SSRF 目标并断言仅最初的公共请求到达传输层
@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/secret",
        "http://169.254.169.254/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fec0::1]/",
        "http://[::127.0.0.1]/",
        "http://[::ffff:0:127.0.0.1]/",
        "file:///etc/passwd",
        "https://user:password@example.com/",
        "http://224.0.0.1/",
    ],
)
async def test_fetch_rejects_unsafe_redirects(
    target: str, transport: Callable[..., object]
) -> None:
    requests = transport(lambda _: httpx.Response(302, headers={"location": target}))
    result = await WebFetchTool().invoke({"url": "https://example.com"})
    assert result.is_error and result.error_type == "permission_denied"
    assert len(requests) == 1
    assert "password" not in result.content


# 功能：验证域名解析中混入私有地址时在发出请求前拒绝
# 设计：模拟同一域名同时解析到公共和私有 IP，防止挑选公共结果掩盖危险 DNS
async def test_fetch_rejects_private_dns(
    public_dns: MagicMock, transport: Callable[..., object]
) -> None:
    public_dns.return_value.append(
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443))
    )
    requests = transport(lambda _: httpx.Response(200, text="secret"))
    result = await WebFetchTool().invoke({"url": "https://example.com"})
    assert result.is_error and result.error_type == "permission_denied"
    assert not requests


# 功能：验证相对重定向以原始域名解析，每一步都重新校验且结果记录最终网址
# 设计：响应一次相对跳转并观察两次 DNS 解析，避免固定 IP 污染来源链接
async def test_fetch_relative_redirect(
    public_dns: MagicMock, transport: Callable[..., object]
) -> None:
    # 根据请求路径返回跳转或正文
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/first":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, text="public document", headers={"content-type": "text/plain"})

    requests = transport(respond)
    result = await WebFetchTool().invoke({"url": "https://example.com/first"})
    assert not result.is_error
    assert json.loads(result.content)["final_url"] == "https://example.com/final"
    assert len(requests) == public_dns.call_count == 2


# 功能：验证重定向循环在固定上限后停止
# 设计：每次返回相同跳转，断言请求次数有界而非依赖超时
async def test_fetch_bounds_redirects(transport: Callable[..., object]) -> None:
    requests = transport(lambda _: httpx.Response(302, headers={"location": "/loop"}))
    result = await WebFetchTool().invoke({"url": "https://example.com"})
    assert result.is_error and "redirect" in result.content.lower()
    assert len(requests) == 6


class _Chunks(httpx.AsyncByteStream):
    # 分块提供正文，验证没有 Content-Length 时仍受实际字节数限制
    async def __aiter__(self):
        yield b"x" * (1024 * 1024)
        yield b"x" * (1024 * 1024)
        yield b"x"


# 功能：验证声明长度与实际流大小均不能突破下载上限
# 设计：分别模拟提前可拒绝和只有读取后才超限的响应，覆盖两条资源保护路径
@pytest.mark.parametrize("declared", [True, False])
async def test_fetch_bounds_download(declared: bool, transport: Callable[..., object]) -> None:
    headers = {"content-type": "text/plain"}
    if declared:
        headers["content-length"] = str(3 * 1024 * 1024)
    transport(lambda _: httpx.Response(200, headers=headers, stream=_Chunks()))
    result = await WebFetchTool().invoke({"url": "https://example.com"})
    assert result.is_error and "2 MiB" in result.content


# 功能：验证非文本类型、HTTP 错误与无正文页面提供可操作的失败信息
# 设计：使用最小响应分别覆盖下载不支持、需要登录和 JavaScript 空壳
@pytest.mark.parametrize(
    ("status", "kind", "body", "hint"),
    [
        (200, "application/pdf", b"%PDF", "content type"),
        (401, "text/html", b"login", "browser"),
        (200, "text/html", b'<html><script src="app.js"></script></html>', "browser"),
        (200, "text/plain", b"", "empty"),
    ],
)
async def test_fetch_reports_unreadable_pages(
    status: int, kind: str, body: bytes, hint: str, transport: Callable[..., object]
) -> None:
    transport(lambda _: httpx.Response(status, content=body, headers={"content-type": kind}))
    result = await WebFetchTool().invoke({"url": "https://example.com"})
    assert result.is_error and hint in result.content.lower()


# 功能：验证标准代理环境变量按原始域名选择且遵守 NO_PROXY
# 设计：清除继承环境后设置本地代理，避免固定公网 IP 使域名免代理规则失效
def test_fetch_proxy_env_uses_original_host(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch.dict(
        "os.environ",
        {"HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": "example.com"},
        clear=True,
    ):
        assert _proxy_for_url(URL("https://example.com/path")) is None
        assert _proxy_for_url(URL("https://other.org")) == "http://127.0.0.1:7890"


# 功能：验证不支持的 SOCKS 代理给出明确错误且不会悄悄绕过代理联网
# 设计：仅配置 ALL_PROXY 并断言传输层未收到请求，防止环境配置被忽略
async def test_fetch_rejects_socks_proxy(transport: Callable[..., object]) -> None:
    requests = transport(lambda _: httpx.Response(200, text="test"))
    with patch.dict("os.environ", {"ALL_PROXY": "socks5://127.0.0.1:7890"}, clear=True):
        result = await WebFetchTool().invoke({"url": "https://example.com"})
    assert result.is_error and "SOCKS" in result.content
    assert not requests


# 功能：验证抓取参数边界在发出请求前被拒绝
# 设计：覆盖负游标、空网址和过长分页请求，复用 Pydantic 标准错误行为
@pytest.mark.parametrize(
    "params",
    [
        {"url": ""},
        {"url": "https://example.com", "start": -1},
        {"url": "https://example.com", "max_chars": 20001},
    ],
)
async def test_fetch_rejects_invalid_params(params: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        await WebFetchTool().invoke(params)
