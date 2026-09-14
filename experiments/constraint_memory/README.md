# 压缩后约束恢复：合成 pilot

这是独立于产品 runtime 的小实验，检验多次滚动摘要后是否仍能选出当前用户确认的设置。
它不修改实际会话、不执行工具、不训练模型，也不代表真实软件任务成功率。
数据协议和六类场景见 [README-data.md](README-data.md)。

## 第一轮预先固定的方案

在看到真实模型结果之前固定：36 题，六类各六题；每题三个压缩检查点；
一个项目配置中的模型、一次重复、temperature=0、thinking disabled；
内存载荷上限 3072 UTF-8 字节，每次模型输出最多 512 tokens，每轮追加 48 行中性工具日志。
主实验比较前三种方法，计划 324 次调用。单独的端点/格式 smoke 不并入主实验。

| 方法 | 可用于最终作答的历史信息 |
|---|---|
| `summary` | 独立滚动摘要，可使用全部内存载荷预算 |
| `retrieval` | 半预算滚动摘要 + BM25 检索的完整原始记录；时间仅打破相关度平局 |
| `versioned` | 与 retrieval 完全相同的摘要 + 按字段保留最新用户确认记录的原始正文 |
| `no_evidence` | 与 versioned 相同的版本头，去掉原始正文；值只能从摘要恢复 |
| `no_versions` | 关闭版本筛选，载荷与 retrieval 相同；用于实现一致性检查，无需重复付费跑 |

所有方法接收相同的公开事件。公开版本信息只有字段键、确认/提议状态和 supersedes 引用，
不带答案值。`answers.json` 中的最终值及旧值只交给评分器。
摘要阶段不知道最终问题，也看不到后续 episode。检索时才使用最终问题和字段键。
增强方法的摘要、来源 ID、角色、版本头和正文共同占用字节上限；放不下的完整记录跳过，
不会给某一组额外的免费原文。摘要超过上限时按 UTF-8 边界截断，完整输出保留在调用日志。

这里的版本索引是**显式结构化生命周期信息下的确定性恢复基线**，并未解决自然语言决策抽取。
数据布局规则、场景由人工模板编写，没有独立 held-out 集；无法据此宣称方法新颖、跨模型泛化或论文贡献。
如果三组都接近满分，应报告当前任务无法区分方法，不筛选只让增强组胜出的题作为主结果。

## 运行

在项目根目录执行，默认离线且不读取模型配置、不调用 API：

```bash
uv run python -m mini_claude.evaluation.constraint_memory \
  --output /tmp/constraint-memory-offline --limit 6
```

真实模型复用现有 `get_config()`、`ANTHROPIC_API_KEY` 与 `ANTHROPIC_BASE_URL`，
通过相同 Anthropic SDK 进行独立的有界非流式调用。每次输出限制不影响产品 provider。
网络错误没有 SDK 或应用层自动重试；默认并发 3，每次 HTTP 超时 60 秒。

```bash
# 端点/格式 smoke；结果单独保存。
uv run python -m mini_claude.evaluation.constraint_memory \
  --live --output experiments/constraint_memory/results/smoke --limit 1 --max-calls 9

# 完整的三组主实验：36 × (3 × 2 次摘要 + 3 次作答) = 324 次调用。
uv run python -m mini_claude.evaluation.constraint_memory \
  --live --output experiments/constraint_memory/results/pilot --max-calls 324

# 独立标注的消融实验；不要把不同配置的结果混作同一次实验。
uv run python -m mini_claude.evaluation.constraint_memory \
  --live --output experiments/constraint_memory/results/ablation \
  --methods retrieval versioned no_evidence --max-calls 216
```

输出目录必须不存在，避免覆盖或混合旧实验。`--limit` 按类别轮转选题。
`--repeats` 控制重复次数；同模板和同题重复不能算作新的独立任务。
后续换模型、预算、提示或任务应使用新目录并重新解释结论。

## 预算与结果解释

- **统一字节上限不是严格的同 token 预算。** 未引入不匹配的 tokenizer；实际 input/output
  usage（含缓存输入）逐次记录。输入按字节数加协议余量预留，再用服务端 usage 结算，
  默认总计量预算 800,000 tokens。预留是保守代理，不声称服务端 tokenizer 的数学上界。
- 调用数和每次输出上限显式设置。在途调用先预留；usage 未知的失败保留预留并单独计数。
  已知 usage 的截断或空响应仍计费。网络中断可能已产生费用，无法将未知费用当成零。
- retrieval/versioned 共用一次小摘要以控制随机性。各方法的管线成本均计入该摘要成本，
  **方法成本相加不等于本次 API 消费**；实际总调用与计量看 summary.json 的 budget。
- 主指标 `task_success` 指三个结构化设置的值全部匹配；不等于真实开发成功。
  次指标为逐字段约束错误、支持该正确值的证据准确率、旧值复活、无效输出和调用失败。
  超时、截断、预算不足均保留在方法分母；全失败时不能解释为方法能力差异。
- 比较同题配对的胜/负/平和每类结果；同时报告 token、p50/p95 管线延迟。
  单模型单次的 36 题 pilot 不作显著性或普遍优越性结论。

每次输出包含：

- `manifest.json`：参数、任务 ID、数据/答案/程序/提示 SHA-256、模型与端点主机。
- `calls.jsonl`：全部公开提示、原始回答、停止原因、实际 usage、失败类型；不保存密钥或隐藏思考。
- `results.jsonl`：每题每组的回答、评分、成本、摘要与最终提示哈希。
- `summary.json` / `REPORT.md`：固定分母汇总和配对比较；离线模式明确不报告模型质量。

## 验证

```bash
uv run pytest tests/unit/test_constraint_memory.py \
  tests/unit/test_constraint_memory_dataset.py -q \
  --cov=mini_claude.evaluation.constraint_memory --cov-report=term-missing --cov-fail-under=80
uv run ruff check src/mini_claude/evaluation \
  tests/unit/test_constraint_memory.py tests/unit/test_constraint_memory_dataset.py
uv run mypy src/mini_claude/evaluation
```

测试覆盖 gold 不改变模型请求、压缩不看未来、共享摘要、所有载荷预算、建议不能覆盖确认、
错误来源独立计分、截断仍计费、并发预算预留和离线 CLI 产物。
