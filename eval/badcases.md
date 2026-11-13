# PipeSched 坏例清单

所有例子来自本次合成数据与评测脚本，不是事后改题。

## 1. 估时：若不分资产类型会串

角色建模真实均值约 5.5 人天，道具建模约 1.8 人天，模板默认却都是 4。
产品默认「任务类型 + 资产类型」同类检索；下面给出测试集里分资产类型与不分的对照。

测试集整体：分资产类型中位数 MAPE = 0.3527，
只按任务类型 = 0.4597。

| 资产 | 类型 | 实际人天 | 分资产类型预测 | 只按任务类型预测 |
| --- | --- | --- | --- | --- |
| c_prop_tea_set | Prop / 建模 | 1.35 | 1.64 | 4.66 |
| c_prop_umbrella_oil | Prop / 绑定 | 1.10 | 0.98 | 3.44 |
| c_prop_sword_heirloom | Prop / 绑定 | 1.20 | 0.98 | 3.44 |
| c_prop_lantern_palace | Prop / 动画 | 2.50 | 2.60 | 4.50 |
| c_character_mage_yun | Character / 动画 | 7.16 | 6.02 | 4.50 |
| c_character_villager_b | Character / 原画 | 4.43 | 2.66 | 1.15 |
| c_character_hero_ling | Character / 原画 | 3.42 | 2.66 | 1.15 |
| c_character_hero_min | Character / 原画 | 2.84 | 2.66 | 1.15 |

## 2. 反话与误导评论（关键词误报）

关键词基线见到「重做」就报警。下面是评测里的假阳性，真值不是当前阻塞。

- **sarcasm** `m_prop_lantern`：建议重做——如果这是三个月前的版本。现在过了。
- **misleading** `m_prop_lantern`：备注：上次重做是因为布线，这次已经修完了。
- **misleading** `m_ui_btn_close`：重做记录见 v3 评论，v4 已通过。
- **sarcasm** `m_ui_icon_shop`：建议重做——如果这是三个月前的版本。现在过了。
- **misleading** `m_ui_banner_event`：备注：上次重做是因为布线，这次已经修完了。
- **misleading** `m_ui_hp_bar`：这个重做是上次的，这次拓扑没问题。
- **misleading** `m_ui_hp_bar`：重做记录见 v3 评论，v4 已通过。
- **misleading** `m_character_mascot_fox`：不要重做，保持现状。
- **sarcasm** `m_character_npc_merchant`：又双叒重做？开玩笑的，这次一次过。
- **sarcasm** `m_character_hero_sage`：建议重做——如果这是三个月前的版本。现在过了。
- **sarcasm** `m_character_crowd_a`：建议重做——如果这是三个月前的版本。现在过了。
- **sarcasm** `m_character_child_npc`：又双叒重做？开玩笑的，这次一次过。

## 3. 宽限期内不报逾期（产品取舍，不是漏报）

- `c_character_grace_window` / 建模：意图 []，规则打出 []。宽限期内负例：due=11-12，宽限 1 天，deadline=11-13，as_of<=deadline，规则按设计不报逾期。这是产品取舍，不是漏报。

## 4. 临界未超阈值

- `m_prop_open_coin` / 制作：意图 []，规则打出 []。临界负例：due=as_of 当天（宽限 0，规则应不报逾期）；实耗/估时=1.15 < 1.2，不报超估。

## 5. 规则层看不到的评论阻塞

- `m_ui_open_banner` / 制作：意图 ['comment_blocker']，规则打出 []。仅评论阻塞：日期与工时看起来正常，备注写「等主美…先搁着」。规则层不应报，关键词/LLM 应报。
- `c_character_wiring` / 绑定：意图 ['comment_blocker']，规则打出 []。仅评论阻塞：规则层日期/工时正常；备注明确「布线要重做」。

## 6. 回顾式：规则漏报后来延期的，或误报却按期完成的

回顾式规则漏报 12 条、误报 17 条（样本 99）。各举若干：

漏报（到期前 2 天规则未响，后来却晚了）：
- `m_character_npc_guard` / 制作（character）due=2026-09-25 done=2026-09-28，当时实耗 1.00 人天 / 估时 2.0，返修 0。
- `c_character_npc_blacksmith` / 建模（character）due=2026-09-30 done=2026-10-06，当时实耗 3.00 人天 / 估时 4.0，返修 0。
- `c_character_npc_diviner` / 贴图（character）due=2026-10-02 done=2026-10-05，当时实耗 2.00 人天 / 估时 3.5，返修 0。
- `c_character_npc_diviner` / 动画（character）due=2026-08-18 done=2026-08-19，当时实耗 1.00 人天 / 估时 6.0，返修 0。
- `c_character_boss_tiger` / 绑定（character）due=2026-10-02 done=2026-10-05，当时实耗 1.00 人天 / 估时 4.0，返修 0。
- `c_character_villager_a` / 贴图（character）due=2026-08-26 done=2026-08-31，当时实耗 0.50 人天 / 估时 3.5，返修 0。
- `c_character_villager_b` / 动画（character）due=2026-07-31 done=2026-08-05，当时实耗 2.00 人天 / 估时 6.0，返修 0。
- `c_character_elder_zhou` / 建模（character）due=2026-07-29 done=2026-08-03，当时实耗 1.00 人天 / 估时 4.0，返修 0。

误报（当时被标风险，后来按期完成）：
- `m_ui_icon_mail` / 原画 规则 ['high_retake']，due=2026-10-05 done=2026-10-05。
- `m_character_mascot_panda` / 制作 规则 ['overrun']，due=2026-10-02 done=2026-10-02。
- `c_character_hero_ling` / 贴图 规则 ['high_retake']，due=2026-10-05 done=2026-10-05。
- `c_character_hero_zhao` / 建模 规则 ['overrun']，due=2026-08-04 done=2026-08-03。
- `c_character_hero_zhao` / 动画 规则 ['overrun', 'dep_blocked']，due=2026-08-28 done=2026-08-28。
- `c_character_npc_blacksmith` / 原画 规则 ['overrun']，due=2026-09-17 done=2026-09-16。
- `c_character_npc_blacksmith` / 绑定 规则 ['high_retake']，due=2026-10-01 done=2026-10-01。
- `c_character_boss_tiger` / 建模 规则 ['high_retake']，due=2026-07-29 done=2026-07-29。

## 7. 依赖用了错误模板会怎样

依赖来自 YAML，不来自模型。若误把手游短管线套到角色项目上，绑定不会再依赖贴图，
`c_character_dep_block` 这类「贴图未完成但绑定已开始」就不会被报。演示时切模板即可对照，不要改代码。

## 8. 模型在工具失败后编日期

本项目日期不经过模型：写回只走确认令牌，估时只走检索。没有「模型编了一个日期」的运行样本，
因为产品路径上就不让模型填日期。这是设计，会在面试里主动讲。

## 9. LLM 评论解读坏例（同一套 149 条金标，题面未改）

模型 `deepseek-ai/DeepSeek-V3.2`。下面是产品输出（护栏之后）相对金标的误报和漏报，以及原始输出上的幻觉标记。

### 9.1 误报（产品输出判为阻塞，金标不是 blocker）

5 条全部是金标 `normal`。引用都对齐原文，不是幻觉；模型把进度受阻的描述读成了「当前阻塞」。这是定义分歧，没有为刷分改金标。

- **normal** `m_ui_open_shop`：还差切图规范，进度比想的慢。 ｜ reason='缺少切图规范' citation='还差切图规范' flags=[] fallback=False
- **normal** `m_prop_open_chest`：细节比估计多，还在抠锁扣。 ｜ reason='细节超出预期，仍在制作中' citation='细节比估计多，还在抠锁扣。' flags=[] fallback=False
- **normal** `m_prop_open_lantern`：按旧稿先做了模型，原画其实还没定。 ｜ reason='依赖未完成的前置环节' citation='原画其实还没定' flags=[] fallback=False
- **normal** `c_character_overrun`：贴图工作量比估计大。 ｜ reason='工作量超出预期' citation='贴图工作量比估计大' flags=[] fallback=False
- **normal** `c_character_dep_block`：先按临时模型绑了，贴图还没过。 ｜ reason='贴图未通过' citation='贴图还没过' flags=[] fallback=False

### 9.2 漏报（金标是 blocker，产品输出未报）

无。

### 9.3 幻觉标记（原始 JSON，护栏之前）

本轮解析成功的输出没有触发幻觉标记。

