# 推送策略（唯一权威说明）

> **只想看「什么情况下会给我发消息、发的是什么、怎么防止被刷屏」——读这一份就够了。**
> 推送规则、文案、闸口全部在 `src/strategy/` 里；`frontend/` 只是本地面板的页面代码，
> 与推送策略无关，不需要看。

本文档描述的是**策略**（什么时候推、推什么、怎么去重），不是操作手册。
怎么装、怎么配 key、怎么加品种，看 [README.md](../README.md)。

---

## 0. 红线（先看这条）

- 系统**只读**行情、**只发**微信提醒。代码里没有任何下单 / 平仓 / 撤单 / 券商接口。
- 大模型**不产生任何行情数字**。所有数字都由 Python 本地算出来或从真实 API 取回来；
  大模型只做「把算好的结果翻译成人话」。大模型输出里出现的每个数字都必须能在真实数据里找到，
  找不到就整段丢弃、回退到确定性模板（见第 5 节）。
- 文案里不允许出现买卖指令。命中 `forbidden_words` 的整条输出被拦截改写（见第 6 节）。
- 「偏多 / 偏空」必须有依据。给不出方向的题材（例如选举）**宁可不推**，绝不硬猜。

---

## 1. 一次推送的完整旅程

所有轨道的推送都经过**同一个出口**：`src/strategy/gate.py` 的 `PushGate.push_status()`。
没有旁路，任何一条微信消息都必须走完这五道关卡：

| 关卡 | 做什么 | 通过条件 | 不通过时 |
|---|---|---|---|
| ① 去重冷却 | 用指纹查 `state/state.json` 的冷却记录 | 该指纹在 `cooldown_minutes`（默认 240 分钟）内没推过 | 返回 `PUSH_COOLDOWN`，不发送（不算失败） |
| ② 重点标记 | `ControlStore.is_priority(track, inst_id)` | 命中重点品种或重点轨道 | 只影响标题加不加 `【重点】`，不影响是否发送 |
| ③ 免打扰 | `ControlStore.is_dnd_active(now)` | 不在免打扰时段；**或**命中了重点 | 非重点 → 转入延迟补发队列，返回 `PUSH_DEFERRED` |
| ④ 真发送 | `ServerChanNotifier.send()` | Server酱返回成功 | 返回 `PUSH_FAILED`，**不写冷却、不落历史**，下一轮重试 |
| ⑤ 落状态 | 发送成功之后才做 | —— | 写冷却指纹 + 追加一条告警历史（面板标注 K 线用） |

返回值一共 5 种：

```python
PUSH_SENT     = "sent"      # 真的发出去了
PUSH_COOLDOWN = "cooldown"  # 冷却中跳过（说明刚推过，可以放心记为已处理）
PUSH_FAILED   = "failed"    # 用户没收到，不能记为已处理
PUSH_SKIPPED  = "skipped"   # 没有可推送的内容（例如事件找不到关联品种）
PUSH_DEFERRED = "deferred"  # 免打扰期间入队，退出时段后补发
```

**为什么必须区分 `PUSH_COOLDOWN` 和 `PUSH_FAILED`**：前者是「这条信号刚推过」，
可以记状态；后者是「用户根本没收到」，如果记成已处理，这条信号就永远丢了。

调用方拿 `_delivery_settled(retry_key, status, force)` 判断「算不算处理完了」：

- 已发送 / 冷却 / 跳过 / 延迟补发 → 处理完了；
- `PUSH_FAILED` → 计数 +1，状态不落盘，下一轮重试；
- 连续失败 `MAX_PUSH_RETRIES = 5` 次 → 放弃重试并记日志；
- 手动 `--force` 的失败**不重试**（用户就在电脑前，自己看得到）。

`dry_run` 与 `--force` 都不受免打扰影响（避免调试时被静音）；
`dry_run` 下连冷却记录和告警历史都不写，避免演练把真实告警压制掉。

多目标推送：`SERVERCHAN_SENDKEY` 支持填多个 SendKey（英文逗号分隔），
推送时对每个密钥独立发送，**任一目标未送达即判定整体失败**。

---

## 2. 五条轨道

| 轨道 | 看什么 | 触发条件一句话 | 指纹前缀 | 配置段 |
|---|---|---|---|---|
| 趋势轨 | 周线均线 | 周线状态（多头/空头/震荡）**发生变化**时才提醒 | `trend` | `trend` |
| 事件轨 | 经济日历 | 关注事件**公布前 180 分钟**发剧本，**公布后**拉真实值对号入座 | `event` | `event`、`calendar` |
| 国际大事轨 | GDELT + RSS 新闻 | 关键词初筛合格的国际新闻，经研判给出明确方向才推 | `world` | `world_events` |
| 重点时段轨 | 盘中 5m K 线 | 配置的重点时段内，短时涨跌幅超过分档阈值 | `session` | `session_watch` |
| 做空观察轨 | 日内压力位 + 盘口 | 价格触及日内压力位且卖压确认，或突破失效位后回落 | `short` | `short_watch` |

### 2.1 趋势轨（周线）

- 状态机：`bull` = 收盘 > 主均线且主均线向上；`bear` = 收盘 < 主均线且主均线向下；两者之外为 `range`。
- 主均线由 `primary_ma` 选 `fast` / `slow`（对应品种配置里的 `ma_periods`）。
- 「站上 / 跌破」带容差 `break_tolerance_pct`（默认 0.3%），避免贴着均线来回抖。
- `only_alert_on_state_change: true` 时，**状态没变不刷屏**——这是它防骚扰的核心机制。
- 补齐短板的是重点时段轨：周线状态切换太慢，例如 2026-10-12→10-13 盘中跌 5.46% 时周线仍是 `bull`。

### 2.2 事件轨（三分支预案）

- 数据来自 AKShare 经济日历，`keywords` + `regions` + `min_importance` 三层筛选。
- 公布前 `pre_alert_minutes = 180` 推「三分支剧本」（强于预期 / 弱于预期 / 符合预期的各自路径）。
- 公布后 `post_check_minutes = 20` 开始拉实际值；数据源的「公布」列会滞后几十分钟到几小时，
  所以最多等到 `post_max_minutes = 1440`（24 小时）。**超过仍未取到实际值就不补推**
  （避免把隔夜旧数据当新闻推），也**绝不编造数字**。
- 缓存跟着事件走：平时 `cache_minutes = 30`，事件公布前后窗口切到 `hot_cache_minutes = 3`。
- 单轮上限 `max_events_per_run = 3`，防止事件扎堆时连环轰炸。
- 品种归属由 `symbol_by_category` 决定（就业/通胀/利率/增长 → TQQQ，原油库存 → CL）。

### 2.3 国际大事轨（新闻）

- 来源全部免 key：GDELT 聚合全球英文媒体为主，BBC / 半岛 RSS 为辅。
- 初筛：`min_score = 2`（标题命中 3 分、摘要命中 1 分），`require_title_hit` 时不接受「摘要蹭词」。
- 方向是**双保险**：确定性历史口径 + DeepSeek 研判。研判没给某个品种明确方向（中性/缺失）→
  **该品种不推**。
- 节奏：`interval_minutes = 20` 研判一轮（新闻不需要 60 秒一抓），`cache_minutes = 15` 取数缓存，
  刷新失败回退旧缓存（不整轮跳过）。
- 上限：单轮 `max_pushes_per_run = 3`，当日 `max_pushes_per_day = 6`。
- 研判是整批 JSON 一次返回，`judge_max_tokens = 3000` 单独放宽（上限太小会被截断成非法 JSON）。
- 大模型不可用时：**退到确定性兜底**（关键词类别 → 固定方向链条），标题原样引用真实新闻标题，
  不做翻译，绝不硬猜。

### 2.4 重点时段轨（盘中波动）

- `session_watch.windows` 定义重点时段（北京时间，支持跨零点，如 `20:00-04:00`），
  时段内轮询间隔从默认 300 秒缩短到 `poll_interval_seconds = 60`。
- 触发用两条尺子：
  - 短时涨跌幅分档 —— `move_thresholds`（默认 30 分钟 0.7% / 60 分钟 1.0% / 240 分钟 1.5%）；
  - 相对「本时段开盘」的累计涨跌幅 —— `since_open_pct = 2.0`。
- 防刷屏做在**幅度分档**上：`magnitude_bucket_pct = 1.0` 表示 −1%~−2% 属同一档，
  同一档在 `cooldown_minutes = 30` 内不重复推；但**幅度升级到更高档位时立即再推一次**
  （例如从 −1% 恶化到 −2%）。

### 2.5 做空观察轨（只提醒，不下单）

- 监控对象刻意与周线关键位互补：周线关键位会丢掉贴近现价的位置，而做空要盯的恰是
  「贴着现价的压力位」——用日内 K 线推出前日高 / 当日高，并叠加 OKX 盘口（`/market/books`）
  与逐笔成交（`/market/trades`）。
- 打分要素与阈值（`short_watch`）：`resistance_max_pct = 3.0`（候选压力位离现价太远就不选）、
  `touch_pct = 0.5`（触及判定）、`pullback_pct = 0.2`（触及后回落才算「被拒绝」）、
  `invalidation_pct = 1.0`（失效位 = 压力位上方该比例处）、
  `imbalance_threshold = -0.2`、`taker_sell_ratio_threshold = 0.55`、`wall_ratio_threshold = 2.0`
  （卖墙）、`counter_trend_penalty = 2`（周线多头时逆势观察扣分）、
  `score_threshold = 4`（**打分达到该值才推送**）。
- 方案A（假突破回落）：观察位只在价格贴近压力位时才说话，价格一离开必然静默，
  而「上破失效位后又跌回」恰好落在静默里。用锚点状态机把它补回来：

  ```text
  watching --收盘站上失效位--> broken --跌回失效位下方 + 卖压确认--> 补发一条回落观察
  ```

- **措辞红线**：只输出「观察区 / 确认条件 / 失效条件」这类客观描述，
  不出现开仓、止损、仓位类引导词汇。

---

## 3. 指纹：去重的键

每条信号在推送前先算一个指纹，闸口拿它做三件事：冷却判定、失败重试计数、落历史。
**指纹格式一旦改动，等于把同一批信号当成新信号重发一遍**，所以 `fingerprint.py` 里是逐字复刻的，
不要顺手改。

| 模板 | 归属 |
|---|---|
| `trend\|inst_id\|state` | 趋势轨（状态切换） |
| `event\|event_fingerprint`、`event\|event_fingerprint\|pre` | 事件轨：公布前剧本 |
| `event\|event_fingerprint\|post\|inst_id` | 事件轨：公布后结果 |
| `world\|url_key\|inst_id` | 国际大事轨 |
| `session\|inst_id\|window_label\|b{bucket}` | 重点时段盘中波动 |
| `short\|inst_id\|{level:.4g}`、`short\|inst_id\|{level:.4g}\|fakebreak` | 做空观察轨（含假突破回落） |
| `retry\|<上面任意一条>` | 失败重试计数键 |

---

## 4. 免打扰与重点提醒（延迟补发）

- **免打扰期间普通推送不丢弃**，而是转入延迟补发队列（落盘 `state/pending.json`，跨进程重启不丢），
  退出免打扰时段后自动补发；补发成功那一刻才落历史。
- 入队时同时记冷却，避免同一信号在免打扰期间每轮重新入队、重新调大模型。
- 补发失败不丢：留在队列里下轮再试，连续失败 5 次后丢弃。
- **重点提醒独立于免打扰**：命中重点品种或重点轨道的信号在免打扰期间照常推送，
  标题统一加前缀 `【重点】`（`wording.py` 的 `PRIORITY_PREFIX`）。
- 配置在本地控制页写进 `state/control.json`（`app.control_file`），监控进程每轮重读，改完即刻生效；
  控制页**只改运行期偏好，不回写 config.yaml**。
- 手动在控制页填写的持仓，优先级高于 `config.yaml` 里的持仓配置。

---

## 5. 大模型在链路里的位置

大模型**只负责措辞**，不负责结论：

| 轨道 | 方向结论来自 | 大模型做什么 |
|---|---|---|
| 趋势轨 / 事件轨 / 重点时段轨 / 做空观察轨 | 固化规则 + 极性表（确定性） | 把算好的结果翻译成人话 |
| 国际大事轨 | 确定性历史口径 + 大模型研判（双保险） | 整批研判并给出方向，给不出就不推 |

- 调用失败 / 超时 / 限流 / 返回不合规时，**自动降级回退到确定性模板**，不阻塞、不中断推送。
- 防编造校验（`guard_against_fabricated_numbers`）：输出里引用的每个数字都必须能在真实数据源里
  找到对应，不合规内容直接丢弃并回退模板。校验同时兼容字段名与对应数值。
- 所有轨道生成消息时附带 AI 思考段落，采用「当前状况 + 判断依据 + 关注要点」三段式；
  大模型未正常返回时自动降级为规则文案标注。

---

## 6. 文案风控

- `allowed_action_text`：建议动作**只允许**出现这一句（默认「建议人工查看/等待确认」），
  严禁买卖措辞。
- `forbidden_words`：命中即拦截重写。默认表含「建议买入 / 建议卖出 / 建议加仓 / 建议减仓 /
  建议抄底 / 建议逃顶 / 建议平仓 / 建议开仓 / 建议止损 / 建议止盈 / 建议重仓 / 建议满仓 /
  必涨 / 必跌 / 一定会涨 / 一定会跌 / 稳赚 / 无风险」。
- `risk_disclaimer`：每条推送都强制带上免责声明（由 `SafetyFilter.ensure_disclaimer` 追加）。
- 推送正文使用 Markdown 排版区分轻重缓急（标题、粗体、emoji、分隔线、空行），
  **不使用 HTML 自定义样式**；清理时只剥离 HTML 标签，保留合法 Markdown。
- 同品种 6 小时内被多条轨道推送时，自动追加**跨轨道口径差异提示**（`memo.py` 的
  `_cross_track_note`），避免用户误以为系统自相矛盾。

---

## 7. 配置项 → 轨道对照

改阈值**只改 `config.yaml`**（模板见 [config.example.yaml](../config.example.yaml)），
不要改代码里写死的常量。

| 想改什么 | 改哪里 |
|---|---|
| 冷却时长（去重窗口） | `notify.cooldown_minutes` |
| 趋势判定容差 / 主均线 / 是否只在状态变化时提醒 | `trend.*` |
| 事件提醒提前量 / 补推窗口 / 单轮上限 | `event.*` |
| 日历关键事件范围与缓存节奏 | `calendar.keywords / regions / min_importance / cache_minutes / hot_cache_minutes` |
| 重点时段与波动阈值 | `session_watch.windows / move_thresholds / since_open_pct / magnitude_bucket_pct` |
| 做空观察打分与阈值 | `short_watch.*` |
| 国际大事节奏与上限、研判批量 | `world_events.*` |
| 推送措辞红线 | `allowed_action_text / forbidden_words / risk_disclaimer` |
| 监控哪些品种 | `symbols`（含 `enabled`） |
| 品种开关 / 免打扰 / 重点 / 持仓（运行期） | 本地控制页 → `state/control.json` |

`config.yaml` 与 `state/` 都在 `.gitignore` 里，不会进公开仓库；
`config.example.yaml` 是公开模板，只含占位值。

---

## 8. 代码地图（看推送策略只需要看这里）

```text
src/strategy/
├── gate.py          # 单点闸口：去重 / 冷却 / 免打扰 / 重点标记 / 失败重试 / 落历史
├── deferral.py      # 免打扰延迟补发队列：入队 / 补发 / 放弃
├── fingerprint.py   # 6 种去重指纹模板（格式锁定，勿改）
├── wording.py       # 措辞常量：状态短名 STATE_SHORT、【重点】前缀
├── memo.py          # 同品种跨轨道口径差异提示
├── helpers.py       # 轨道共用纯计算（bar_minutes / gap_pct）
└── tracks/          # 五条轨道的触发规则与文案
    ├── trend.py     # 趋势轨
    ├── event.py     # 事件轨（含关联品种 / 关键位助手，其它轨复用）
    ├── world.py     # 国际大事轨
    ├── session.py   # 重点时段轨
    └── short.py     # 做空观察轨
```

边界约定（由 `tests/test_safety.py::test_strategy_isolation` 守护）：

- `src/strategy/**` **不依赖前端**（不 import `src.dashboard`、`http.server`、`frontend`），
  也不反向依赖入口 `run.py`——避免循环依赖；
- 策略层**不含任何下单 / 撤单 / 券商接口**特征（`/api/v5/trade`、`place_order`、`ccxt` 等）；
- `run.py` 只做编排（取数、缓存、调度、CLI）与委托，并保留常量 re-export
  （`PUSH_*` / `MAX_PUSH_RETRIES` / `PRIORITY_PREFIX` / `STATE_SHORT`），旧调用方不受影响。

编排层与取数留在 `run.py` 的部分（不属于策略）：
`_active_symbols`、`_weekly_candles`、`_calendar_events`、`_world_items`、`current_window`、
`check`、`one_pass`、`main`，以及 `src/` 下的取数（`datasource/`）、确定性算法（`modules/`）、
风控渲染（`alert.py`）、状态记忆（`state.py`）、控制项存储（`control.py`）。
