# 版本与发布

## 编号约定

日常 commit/push 不必逐次升版本。完成一批功能并验证后发布一版，
在 [CHANGELOG.md](../CHANGELOG.md) 说明新增、修复、升级方式和已知限制。

- 稳定版采用 `主版本.次版本.修订号`：不兼容变化升主版本，兼容新增功能升次版本，
  兼容问题修复升修订号。例如 `2.0.0 → 2.0.1 → 2.1.0 → 3.0.0`。
- Python 测试版写作 `2.0.0b1`，对应 Git 标签 `v2.0.0-beta.1`；
  下一次测试版为 `2.0.0b2` / `v2.0.0-beta.2`。稳定后发布 `2.0.0` / `v2.0.0`。
- S8、S9 等编号只用于内部里程碑，不直接转换为产品版本。
- 已公开的标签不改名、不改指向。旧 `v1.0.0` 虽无 GitHub Release，仍保留其历史含义。

当前 2.0 测试版包含相对旧标签的 CLI 运行方式、事件字段和文件路径规则变化，
因此递增主版本；测试版身份不表示稳定性承诺。
规则参考 [SemVer](https://semver.org/lang/zh-CN/) 与
[Python 版本规范](https://packaging.python.org/en/latest/specifications/version-specifiers/)。

## 发布前

1. 选定本次发布包含的提交，核对本地与 GitHub 的差异：

   ```bash
   git fetch origin
   git log --oneline origin/main..HEAD
   git status --short
   ```

2. 同步 `pyproject.toml` 的 `project.version`、`src/mini_claude/__init__.py` 的
   `__version__`、README 的版本与示例、CHANGELOG 的版本标题。
   当前 `uv.lock` 被 Git 忽略，依赖同步后在本地更新，不将其当成已随发布跟踪的锁文件。

3. 检查发布内容：

   ```bash
   uv sync
   uv run mini --version
   make verify-s0
   node --test tests/web/*.test.mjs
   uv build
   ```

   CLI 输出和构建包的版本应一致。检查生成的 wheel 包含 Web 静态文件、内置 Skills 和角色配置。
   桌面或联网改动还需按 [桌面文档](DESKTOP.md) / [联网文档](NETWORKING.md) 做对应验收。
   未通过的检查应先处理或明确记录，不能写成已验证通过。

4. 发布当天将 CHANGELOG 的“待发布”替换为实际日期，移除“计划对应”“尚未创建”等
   准备状态说明，并更新 README 的发布状态；
   从本次 CHANGELOG 节复制发布说明到临时文件 `/tmp/miniclaude-release-notes.md`，
   保留升级方法和限制。提交版本文件、README 与 CHANGELOG 的改动。

## 发布到 GitHub

以下为当前首个测试版的命令。在选定的发布提交上执行；GitHub Release 基于该标签。

```bash
git tag -a v2.0.0-beta.1 -m "MiniClaude 2.0.0 Beta 1"
git push origin main
git push origin v2.0.0-beta.1
gh release create v2.0.0-beta.1 --verify-tag --prerelease \
  --title "MiniClaude 2.0.0 Beta 1" \
  --notes-file /tmp/miniclaude-release-notes.md
```

发布后在 GitHub 检查标签提交、测试版标记与发布说明，再在 CHANGELOG 顶部新增
“未发布”节收集后续变化。稳定版使用对应的正式标签，并省略 `--prerelease`。
详见 [GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。
