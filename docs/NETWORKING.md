# 联网能力

MiniClaude 将搜索、网页读取和浏览器交互注册为独立工具，由 agent 按任务选择。
工具沿用现有 TUI/桌面的执行事件、权限审批和停止流程。

## 上游选择

本次集成复用正式发布包，MiniClaude 只维护适配层，没有复制上游源码或引入另一套 agent loop。

| 能力 | GitHub 上游 | 本次固定版本 | 许可证 |
| --- | --- | --- | --- |
| 搜索 | [deedy5/ddgs](https://github.com/deedy5/ddgs) | 9.16.0 | MIT |
| 网页正文 | [adbar/trafilatura](https://github.com/adbar/trafilatura) | 2.2.0 | Apache-2.0 |
| 浏览器 | [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) | 0.0.80 | Apache-2.0 |

DDGS 无需 API key，通过搜索后端返回结果；后端可能限流或改变接口，可用性不等于商业 API 的保证。
当前使用该版本实际启用的 Yahoo、DuckDuckGo、Brave 网页搜索后端，
避免自动模式将百科结果优先用于通用搜索。这些公共网页后端仍可能限流或暂时无结果。
选定版本已移除旧版 DHT 缓存功能。
Trafilatura 从已下载的 HTML 提取正文和链接，不执行 JavaScript。
Playwright MCP 复用微软的页面快照和交互实现。

也评估了 [Brave 官方 MCP](https://github.com/brave/brave-search-mcp-server)、
[MCP Fetch](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch)、
[Crawl4AI](https://github.com/unclecode/crawl4ai) 和
[browser-use](https://github.com/browser-use/browser-use)。Brave 需要额外 API key；
其余方案分别需要额外抓取进程、较完整的爬虫运行时或另一套浏览器 agent。
本次选择适合当前 Python 工具接口的较小依赖组合。

## 安装和使用

```bash
uv sync
```

搜索和网页抓取随 Python 依赖安装。浏览器需要 Node.js 20+、`npx` 和 Chrome。
首次浏览器工具调用会获取固定版本的 `@playwright/mcp@0.0.80` 并启动独立浏览器；
普通搜索、抓取和本地任务不会启动浏览器。可提前下载和检查依赖：

```bash
npx --yes @playwright/mcp@0.0.80 --help
```

重启正在运行的 MiniClaude core 后，在 TUI 或桌面对话中使用：

- “搜索 Python asyncio TaskGroup 的官方文档，读取来源后解释，并附链接。”
- “读取 https://example.com ，总结这个页面。”
- “用浏览器打开这个网页，查看点击展开后出现的内容。”

模型可调用的接口：

| 工具 | 作用 |
| --- | --- |
| `web_search(query, max_results=5)` | 搜索，返回标题、网址和摘要，最多 10 条 |
| `web_fetch(url, start=0, max_chars=6000)` | 读取 HTML/纯文本，返回正文、来源和分页游标 |
| `browser_navigate` | 导航到 HTTP(S) 页面，随后调用 `browser_snapshot` 读取内容 |
| `browser_snapshot` | 读取页面结构，按目标缩小范围 |
| `browser_click` / `browser_type` | 点击或输入，使用上一步快照中的目标 |
| `browser_press_key` / `browser_wait_for` | 按键、等待页面文字 |
| `browser_close` | 关闭当前浏览器 |

知道 URL 时可以直接抓取；需要寻找来源时搜索；需要 JavaScript、登录或交互时使用浏览器。
搜索摘要与完整网页内容分开看待，最终回答应引用实际读取的来源。
PDF、图片等二进制内容不在本次 `web_fetch` 支持范围内。

## 配置

可在 `.mini/config.toml` 或 `~/.mini/config.toml` 中设置：

```toml
[network]
enabled = true
browser_enabled = true
browser_headless = true
browser_executable_path = ""
```

对应环境变量：`MINI_NETWORK_ENABLED`、`MINI_BROWSER_ENABLED`、
`MINI_BROWSER_HEADLESS`、`MINI_BROWSER_EXECUTABLE_PATH`。环境变量覆盖 TOML。
总开关控制本次内置联网工具，不限制另外配置的 MCP 服务或 Bash 命令联网。

浏览器默认无窗口；设置 `browser_headless = false` 可显示独立窗口，
在当前任务内手动完成登录。`browser_executable_path` 可指定 Chrome 可执行文件。
每个主任务与子任务使用独立浏览器，任务成功、失败或取消后自动释放。
**浏览器页面、登录状态不跨用户消息保留**；下一轮任务会建立新浏览器。
同一浏览器的动作顺序执行；搜索和独立网页读取仍可并行。

搜索代理使用上游支持的 `DDGS_PROXY`。
抓取按原始目标域名读取 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 与 `NO_PROXY`，
代理地址需为 HTTP(S) 代理；本次抓取器不支持 SOCKS 代理，并会明确报错。
HTTPS 抓取保留 TLS 主机名验证，不通过关闭证书验证解决网络问题。

部分 VPN 使用 `198.18.0.0/15` 虚拟 IP。仅当域名的所有系统 DNS 结果均属于该范围时，
抓取器会通过 [Cloudflare DNS-over-HTTPS](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)
查询公开 IPv4 地址，重新校验后连接。只发送目标域名，不发送网页路径、查询参数或正文。
直接输入的虚拟 IP、私网地址及公私混合 DNS 结果仍然拒绝；DNS 服务不可达时明确失败。

## 权限与结果

公开搜索和抓取默认允许，在只读会话中也可使用。
抓取仅访问公共 HTTP(S) 目标，每次重定向都检查地址，并固定到已校验的公共 IP，
下载上限为 2 MiB，最多跟随 5 次重定向。
私网和本机页面应通过经过审批的浏览器操作访问。

浏览器工具沿用现有审批模式，默认询问；只读模式拒绝浏览器操作，
完全访问模式直接放行。现有“始终允许”按工具名生效，应根据实际任务选择授权范围。
Playwright 会话隔离不等同于网络沙箱。

网页和搜索结果标注为外部不可信资料，并保留来源与抓取时间。
抓取支持分页；浏览器快照截断时提示缩小目标范围。
浏览器和 MCP 工具错误会明确传回 agent；这些工具不会因运行错误自动重复执行，
避免重复提交。结果不明确时，模型应先查看页面状态。

无角色子代理获得独立的联网工具；planner/reviewer 角色只增加搜索和抓取，
executor 角色同时可用浏览器交互。自定义角色仍由其 `allowed_tools` 白名单决定。

## 验证

```bash
uv run pytest tests/unit/test_web_search.py tests/unit/test_web_fetch.py \
  tests/unit/test_browser_tools.py tests/unit/test_mcp_client.py \
  tests/unit/test_network_integration.py tests/unit/test_tool_retry.py
```

自动测试不依赖外网账号，覆盖搜索接口、真实 HTML 正文提取、地址和下载限制、
MCP 错误、提交不重试、浏览器隔离与取消清理。真实联网可用性需要在运行环境验证，
搜索后端、代理和浏览器安装都会影响结果。

本次本机验证（2026-09-13）：94 项相关测试通过，所测工具模块合计覆盖率 86%；
完整 Python 测试 520 项通过，6 项旧压缩测试因事件循环假设失败，
已在改动前的 `3f6ca4d` 版本独立复现同样的 6 项失败。
严格类型检查通过，修改范围的 lint 通过。
真实搜索曾返回 Python 官方文档，正文抓取与独立浏览器交互均通过；搜索同时观察到间歇性空结果。
