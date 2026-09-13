from __future__ import annotations

import json
import socket
from collections.abc import Callable, Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from mini_claude.core.tools.builtin.web_fetch import WebFetchTool, _proxy_for_url

_PUBLIC_IP = "93.184.216.34"
_ARTICLE = (
    '<html><head><title>Research notes</title></head><body><nav>Navigation</nav><article>'
    '<h1>Research notes</h1><p>These research notes explain how safe network access works '
    'and provide sufficient context to preserve the main article during extraction.</p>'
    '<p>Read the <a href="https://example.org/reference">reference document</a> for '
    'additional details and examples of network behavior.</p></article></body></html>'
)


# 提供公共 DNS 解析结果，阻止测试发起真实域名查询
@pytest.fixture(autouse=True)
def public_dns() -> Iterator[MagicMock]:
    with patch("socket.getaddrinfo", return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 443)),
    ]) as resolver:
        yield resolver


# 通过真实 HTTPX 模拟传输保留请求与流式读取行为，完全禁止外部网络
@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    original_client = httpx.AsyncClient

    # 为当前测试安装响应处理器并收集原始请求
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        # 记录实际传入传输层的 URL 与扩展后返回模拟响应
        def record(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return handler(request)

        # 用无网络传输替换客户端，其他公开参数保留原行为
        def client(**kwargs: object) -> httpx.AsyncClient:
            kwargs.pop("proxy", None)
            kwargs.pop("trust_env", None)
            return original_client(transport=httpx.MockTransport(record), trust_env=False, **kwargs)

        monkeypatch.setattr("mini_claude.core.tools.builtin.web_fetch.httpx.AsyncClient", client)
        return requests

    return install


# 功能：验证真实正文抽取保留标题与链接，连接固定到已校验 IP 并保留 TLS 主机名
# 设计：拦截传输层而不替换提取器，同时验证抽取效果及 DNS 重绑定防护的实际请求
async def test_fetch_extracts_article_and_pins_connection(transport: Callable[..., object]) -> None:
    requests = transport(lambda _: httpx.Response(200, text=_ARTICLE, headers={"content-type": "text/html"}))
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


# 功能：验证纯文本分页可连续读取，末页正确标记结束
# 设计：使用唯一字符序列检查页边界和游标，避免只判断字符串长度
async def test_fetch_paginates_plaintext(transport: Callable[..., object]) -> None:
    content = "abcdefghij"
    transport(lambda _: httpx.Response(200, text=content, headers={"content-type": "text/plain"}))
    first = json.loads((await WebFetchTool().invoke({"url": "https://example.com", "max_chars": 4})).content)
    last = json.loads((await WebFetchTool().invoke({"url": "https://example.com", "start": 4, "max_chars": 10})).content)
    assert first["content"] == "abcd" and first["next_start"] == 4 and first["truncated"]
    assert last["content"] == "efghij" and last["next_start"] is None and not last["truncated"]


# 功能：验证外部页面不能重定向到内网、凭据地址或非 HTTP 资源
# 设计：枚举常见 SSRF 目标并断言仅最初的公共请求到达传输层
@pytest.mark.parametrize("target", [
    "http://127.0.0.1/secret", "http://169.254.169.254/", "http://10.0.0.1/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "file:///etc/passwd",
    "https://user:password@example.com/", "http://224.0.0.1/",
])
async def test_fetch_rejects_unsafe_redirects(target: str, transport: Callable[..., object]) -> None:
    requests = transport(lambda _: httpx.Response(302, headers={"location": target}))
    result = await WebFetchTool().invoke({"url": "https://example.com"})
    assert result.is_error and result.error_type == "permission_denied"
    assert len(requests) == 1
    assert "password" not in result.content


# 功能：验证域名解析中混入私有地址时在发出请求前拒绝
# 设计：模拟同一域名同时解析到公共和私有 IP，防止挑选公共结果掩盖危险 DNS
async def test_fetch_rejects_private_dns(public_dns: MagicMock, transport: Callable[..., object]) -> None:
    public_dns.return_value.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)))
    requests = transport(lambda _: httpx.Response(200, text="secret"))
    result = await WebFetchTool().invoke({"url": "https://example.com"})
    assert result.is_error and result.error_type == "permission_denied"
    assert not requests


# 功能：验证相对重定向以原始域名解析，每一步都重新校验且结果记录最终网址
# 设计：响应一次相对跳转并观察两次 DNS 解析，避免固定 IP 污染来源链接
async def test_fetch_relative_redirect(public_dns: MagicMock, transport: Callable[..., object]) -> None:
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
@pytest.mark.parametrize(("status", "kind", "body", "hint"), [
    (200, "application/pdf", b"%PDF", "content type"),
    (401, "text/html", b"login", "browser"),
    (200, "text/html", b'<html><script src="app.js"></script></html>', "browser"),
    (200, "text/plain", b"", "empty"),
])
async def test_fetch_reports_unreadable_pages(status: int, kind: str, body: bytes, hint: str,
                                           transport: Callable[..., object]) -> None:
    transport(lambda _: httpx.Response(status, content=body, headers={"content-type": kind}))
    result = await WebFetchTool().invoke({"url": "https://example.com"})
    assert result.is_error and hint in result.content.lower()


# 功能：验证标准代理环境变量按原始域名选择且遵守 NO_PROXY
# 设计：清除继承环境后设置本地代理，避免固定公网 IP 使域名免代理规则失效
def test_fetch_proxy_env_uses_original_host(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch.dict("os.environ", {"HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": "example.com"}, clear=True):
        assert _proxy_for_url(httpx.URL("https://example.com/path")) is None
        assert _proxy_for_url(httpx.URL("https://other.org")) == "http://127.0.0.1:7890"


# 功能：验证抓取参数边界在发出请求前被拒绝
# 设计：覆盖负游标、空网址和过长分页请求，复用 Pydantic 标准错误行为
@pytest.mark.parametrize("params", [
    {"url": ""}, {"url": "https://example.com", "start": -1},
    {"url": "https://example.com", "max_chars": 20001},
])
async def test_fetch_rejects_invalid_params(params: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        await WebFetchTool().invoke(params)
