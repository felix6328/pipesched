# 费用口径

规则扫描和 Gazu 调用在本地，API 费用为 0。

LLM 只应处理新增或变更的评论，不要把全量任务每天塞进上下文。密钥只从 `.env.local` 的 `SILICONFLOW_API_KEY` 这一行读取（见 `pipesched/envload.py`），不要把整个文件 load_dotenv 进进程，也不要使用 `DEEPSEEK_*`（第三方中转）。`.env.local` 已在 `.gitignore`，不得提交、不得打进 zip。

## v2 实测（2026-11-14，Asia/Shanghai）

- 模型：`deepseek-ai/DeepSeek-V3.2`（硅基流动官方 `https://api.siliconflow.cn/v1`，账号 `/v1/models` 列表可见）。
- 同一套 149 条金标、29 条不重复原文。实际 HTTP 调用 **44** 次（并发下对同一原文有重复请求；token 按 44 次返回值计）。API 失败 0，解析失败 0，回退 0。
- API 返回：prompt_tokens=10502，completion_tokens=1276，cache_hit_tokens=0，合计 11778。
- 标价（https://www.siliconflow.cn/pricing ，查阅 2026-11-14）：输入 ¥4.00/M、输出 ¥6.00/M、缓存命中 ¥0.40/M。估算 **¥0.049664**。这是按返回 token 与标价估算，不是账单原件；标价会变动。
- 单次调用时延 P50=3520.53 ms，P95=10390.92 ms。产品路径应异步 / 影子，不要堵住工作台。
- 用量明细在本地 `data/cost/llm.jsonl`（gitignore，不含密钥）。
