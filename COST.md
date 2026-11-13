# 费用口径

规则扫描和 Gazu 调用在本地，API 费用为 0。

LLM 只应处理新增或变更的评论，不要把全量任务每天塞进上下文。本仓库默认 `llm.backend: auto`：没有 `OPENAI_API_KEY` 则不调用，**本次评测费用为 0**。

若以后打开真实模型，用 `data/cost/llm.jsonl` 记 token 和时延，简历只写账单校准过的数字，不要把假设单价当成结果。
