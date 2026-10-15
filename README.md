# 双轨交易信号监控与告警系统

> **这不是自动交易机器人。**
> 本系统只做两件事：① 只读地拉取公开行情/经济数据；② 把信号推送到你的微信。
> 它**没有任何下单、平仓、撤单能力**，也不连接任何券商或交易所的交易接口。
> 最终的买卖决策永远由你本人做出。

---

## 目录

- [一、它是怎么保证不说假话的](#一它是怎么保证不说假话的)
- [二、系统要求](#二系统要求)
- [三、一键安装](#三一键安装)
- [四、怎么填 key](#四怎么填-key)
- [五、怎么加品种](#五怎么加品种)
- [六、怎么设支撑位/压力位](#六怎么设支撑位压力位)
- [七、怎么填你自己的持仓数据](#七怎么填你自己的持仓数据)
- [八、历史数据来源：用美股本土数据补历史](#八历史数据来源用美股本土数据补历史)
- [九、重点时段盘中波动监控（防急跌）](#九重点时段盘中波动监控防急跌)
- [十、本地可视化面板（只读 K线 + 历史告警标注）](#十本地可视化面板只读-k线--历史告警标注)
- [十一、开机自启 / 停止 / 看状态](#十一开机自启--停止--看状态)
- [十二、日志在哪](#十二日志在哪)
- [十三、命令速查](#十三命令速查)
- [十四、目录结构](#十四目录结构)
- [十五、已知限制（重要，请务必读）](#十五已知限制重要请务必读)
- [十六、常见问题](#十六常见问题)
- [十七、完整流程文档与部署提示词](#十七完整流程文档与部署提示词)

---

## 一、它是怎么保证不说假话的

行情数字全部来自真实 API，大模型只负责"把算好的结果翻译成人话"。具体分工：

| 环节 | 谁来做 | 代码位置 |
|---|---|---|
| 拉现价 | Python 调 OKX 只读接口 | `src/datasource/okx.py` |
| 拉周K历史 | OKX 周K，或美股本土 TQQQ 周K（补历史深度） | `src/datasource/okx.py`、`src/datasource/us_stock.py` |
| 算均线、判破位、判趋势状态 | Python 本地确定性计算 | `src/modules/trend.py` |
| 推导支撑/压力位 | Python 由真实K线统计 | `src/modules/levels.py` |
| 算持仓浮盈亏 | Python 本地公式 | `src/alert.py` |
| 拉公布时间/预期值/实际值 | AKShare 经济日历 | `src/datasource/econ_calendar.py` |
| **翻译成人话 + 三分支剧本** | DeepSeek | `src/llm/deepseek.py` |

**关键防线**：大模型返回的文本里，每一个数字都会和"我喂给它的真实数据"逐一比对。
只要出现一个数据源里没有的数字（哪怕只是它自己算的一个"大概 5.2"），
这条输出就会被**整条丢弃**，改用本地规则模板——宁可读起来朴素，也绝不放行假数字。

这条防线有自动化测试证明有效：

```powershell
python tests\test_safety.py     # 224 项断言，覆盖红线1/2/3
```

---

## 二、系统要求

- **操作系统**：Windows 10 / 11（已在 Windows 10 专业版 19045 上实测部署）
- **Python**：3.11+（`install.ps1` 会在缺失时自动用 winget 装 3.12）
- **网络**：需要能访问 `www.okx.com`、`api.deepseek.com`、`sctapi.ftqq.com`、
  `query1.finance.yahoo.com`（美股历史，可退回 `stooq.com`）
  以及 AKShare 用到的财经数据站点
- **权限**：普通用户即可；注册开机自启需要管理员

---

## 三、一键安装

用**管理员身份**打开 PowerShell，执行：

```powershell
cd "D:\deepseek harness\TRADE"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1
```

脚本会依次完成：

1. 检测 Python，没有就用 winget 安装 Python 3.12
2. `pip install -r requirements.txt`
3. 生成 `config.yaml`（从 `config.example.yaml`，**已存在则不覆盖**）
4. 生成 `.env`（从 `.env.example`，**已存在则不覆盖**）
5. 自动跑一遍自检并打印报告

---

## 四、怎么填 key

系统**不会**把 key 硬编码在任何文件里。有两种方式，任选一种：

### 方式 A：写在 `.env` 文件里（推荐）

编辑项目根目录的 `.env`：

```ini
DEEPSEEK_API_KEY=sk-你的真实key
SERVERCHAN_SENDKEY=SCT你的真实sendkey
```

- 这个文件已被 `.gitignore` 排除，不会被提交
- **注意**：`SERVERCHAN_SENDKEY` 只填 `SCT` 开头的那串，不要带 `sctp` 前缀、不要带网址
- **推给多个用户**：把各自的 SendKey 用英文逗号（或分号/空格）连起来写进同一个变量即可，
  系统会对每个 key 各推一次。只要有一个没送达就算失败，会按重试规则重发，不会漏掉某个人。
  每个用户需自己用微信登录 [sct.ftqq.com](https://sct.ftqq.com) 领取 SendKey。
  例：`SERVERCHAN_SENDKEY=SCT第一个key,SCT第二个key`

> 提醒：Server酱免费额度是 **5 条/天**，推 N 个用户就是每条信号消耗 N 条配额，
> 多用户场景下建议升级订阅会员（每日 API 请求 1000 次）。

### 方式 B：设置系统环境变量

```powershell
# 只对当前用户生效（需要重新打开终端）
[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-你的真实key", "User")
[Environment]::SetEnvironmentVariable("SERVERCHAN_SENDKEY", "SCT你的真实sendkey", "User")

# 对所有用户/服务生效（开机自启以 SYSTEM 身份运行时必须用这个）
[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-你的真实key", "Machine")
[Environment]::SetEnvironmentVariable("SERVERCHAN_SENDKEY", "SCT你的真实sendkey", "Machine")
```

> **以 SYSTEM 身份开机自启时的坑**：SYSTEM 账户看不到你个人级别（User）的环境变量。
> 如果你用 `register_autostart.ps1 -RunAs system`，请务必使用 **方式 A（`.env`）**
> 或设置 **Machine 级别**环境变量。

填好后验证：

```powershell
python run.py --check       # 看 [1] 和 [2] 是否都打 ✓
python run.py --test-push   # 真的往微信推一条测试消息
```

---

## 五、怎么加品种

编辑 `config.yaml` 的 `symbols` 列表，追加一项：

```yaml
  - name: "SOL 索拉纳（OKX 现货）"      # 显示名，随便起
    inst_id: "SOL-USDT"                  # 【必须】OKX 真实交易对代码
    inst_type: "SPOT"                    # SPOT=现货 / SWAP=永续合约
    enabled: true                        # false = 暂时不监控
    history_source: "okx"                # 加密原生品种用 okx；有美股对应物的才用 us_stock
    ma_periods: [20, 30]                 # 周线均线周期
    cross_pairs: [[20, 30]]              # 判断哪两条均线金叉/死叉
    levels:
      source: "auto"
      lookback_weeks: 12
      min_distance_pct: 1.5
      manual_support: []
      manual_resistance: []
    position: {}                         # 没持仓就这样留空
```

### `inst_id` 怎么写？

**不要猜。** 用下面命令列出 OKX 上所有真实交易对：

```powershell
python -c "import sys;sys.path.insert(0,'.');from src.datasource.okx import OkxClient;c=OkxClient();print([i['instId'] for i in c._api('/api/v5/public/instruments',{'instType':'SPOT'}) if 'SOL' in i['instId']])"
```

把 `SPOT` 换成 `SWAP` 可查永续合约。**写错的 `inst_id` 会让系统明确报错并跳过该品种，而不是瞎猜一个。**

### 均线周期要注意

均线需要足够的历史K线才能算。例如 `MA20` 需要至少 20 根已收盘周K。
历史不够时，系统会在告警里显式写「MA20=数据不足」，**不会伪造一个均线值**。

加密原生品种（BTC/ETH 等）在 OKX 上历史都很长，没问题。
但**代币化股票/杠杆 ETF 代币**往往上市很晚，历史很短——
这种情况下请参考[第八节](#八历史数据来源用美股本土数据补历史)，改用美股本土数据。

查某品种到底有多少根周K：

```powershell
python run.py --check      # 输出里会标出每个品种的已收盘周K根数
```

---

## 六、怎么设支撑位/压力位

有两种方式，可以混用：

### 自动推导（默认）

`source: "auto"` 时，系统用该品种**真实周K**的最高/最低/收盘价做统计（区间极值 + 摆动高低点）推导出支撑/压力位。
所有数字都可追溯，并在告警里标注「由真实周K算法推导」。

调节推导窗口：

```yaml
    levels:
      source: "auto"
      lookback_weeks: 12      # 用最近 12 周；调大 = 参考更长期的关键位
```

### 手动指定（覆盖/补充）

你自己看盘画出来的位置更准，直接填进去：

```yaml
    levels:
      source: "auto"
      lookback_weeks: 12
      manual_support: [78.50, 75.00, 71.20]      # 支撑位，随便填几个
      manual_resistance: [85.00, 88.80]          # 压力位
```

填了手动值之后，系统会把手动值和自动推导值**合并**，并在告警里标注「配置指定」。
告警里只显示离现价最近的那一个支撑和一个压力。

---

## 七、怎么填你自己的持仓数据

**这类数据系统推导不出来，只能你给。** 在品种下面这样写：

```yaml
    position:
      side: "long"          # long=多单 / short=空单
      entry_price: 100.0    # 开仓均价（示例数字，换成你自己的）
      margin_usdt: 100.0    # 占用保证金（USDT）
      leverage: 4           # 杠杆倍数
      note: "4x 杠杆多单"
```

填了之后，每条趋势告警里会多出一行：

```
【你的持仓】多单 4x｜开仓价 100.0｜保证金 100.0U｜当前浮亏 12.80U（约 -12.80% 保证金收益，价格-3.20%）
```

计算方式是本地确定性公式（不涉及任何交易接口）：

```
名义价值 = 保证金 × 杠杆
数量     = 名义价值 ÷ 开仓价
浮盈亏   = (现价 − 开仓价) × 数量 × (+1 多单 / −1 空单)
```

没持仓的品种把 `side`/`entry_price` 留空即可，系统不会瞎编。

---

## 八、历史数据来源：用美股本土数据补历史

### 为什么需要这个

OKX 的 `TQQQ-USDT-SWAP` **2026-06 才上线**，只有 **15 根已收盘周K**，
按 OKX 自己的数据根本算不出 MA20/MA30。

而美股本土 TQQQ 有多年历史，所以系统支持把周K历史切到美股本土数据：

```yaml
    history_source: "us_stock"     # okx = 用 OKX 周K；us_stock = 用美股本土周K
    us_symbol: "TQQQ"              # Yahoo/Stooq 上的美股代码
    us_history_range: "5y"         # 拉多少年
    divergence_warn_pct: 3.0       # 与 OKX 现价偏离超此值就在告警里警示
```

切换后 TQQQ 的周K从 15 根变成 **261 根**，MA20/MA30 立刻可用。

**实时价永远用 OKX 的真实价格**，美股数据只用来补历史均线。
告警里会显式标注均线来自哪个数据源，以及两边当前偏离多少：

```
【周线均线】MA20=74.4260↑，MA30=69.2930↑
【历史数据来源】美股本土 TQQQ（Yahoo/Stooq）｜已收盘周K 261 根
【跟踪偏差】美股周收盘 81.28（2026-10-05）vs OKX 现价 81.06，偏离 -0.27%
```

偏离超过 `divergence_warn_pct` 时会追加警示，提示你人工确认两个市场是否仍同步。

### 两者走势到底一不一致？（实测数据）

用 **70 个共同交易日**逐一比对（`tools/track_check.py`）：

| 指标 | 实测值 | 含义 |
|---|---|---|
| 日均价差 | **+0.205%** | 价格水平基本贴住 |
| 价差标准差 | 0.790% | 日常波动很小 |
| 最大价差 | 3.505% | 极端时会有 3.5% 偏离 |
| 日收益相关系数 | **0.9451** | 高度相关 |
| 同向日占比 | **94.2%**（65/69） | 方向基本一致 |
| 日均收益偏差 | 0.755 个百分点 | 有真实损耗 |
| **最大日收益偏差** | **4.441 个百分点** | 个别日子会明显跑偏 |

**趋势状态层面**（这才是真正影响告警的）：

| 对比项 | 结果 |
|---|---|
| 重叠周内 MA10 趋势状态一致率 | **100%**（5/5） |
| 重叠周内 MA5 趋势状态一致率 | 70% |

### 结论与注意事项

**结论：可以拿来补历史。** 两者价格水平和方向高度一致，
尤其 MA10 以上的趋势状态完全一致——而趋势轨用的正是周线均线，不是日内波动。

**但必须知道三点：**

1. **两者不是同一个东西。** 一个是 3 倍杠杆 ETF（有实际持仓成本、每日再平衡），
   一个是加密交易所的永续合约（有资金费率、有溢价、7×24 交易）。
   平均 0.755pp 的日收益偏差就是这么来的。
2. **周K口径不同。** 美股周K是周一到周五；OKX 周K是周日 16:00 UTC 到下周日 16:00 UTC，
   **包含周末加密盘的波动**。所以两边周K收线本来就不完全等价。
3. **跟踪可能失效。** 如果某天永续出现大幅溢价/折价，走势就会脱钩。
   系统用 `divergence_warn_pct` 帮你盯着这件事。

### 哪些品种能用

| 品种 | 能否用美股数据补 | 说明 |
|---|---|---|
| `TQQQ-USDT-SWAP` | ✅ 可以 | 对应美股 `TQQQ`，已验证 |
| `ETH-USDT` | ❌ 用不上 | 加密原生资产，没有美股对应物 |
| `CL-USDT-SWAP` | ⚠️ 暂未启用 | 原油有美股 `USO`，但 USO 的价格水平与 OKX 的 CL 不是 1:1（148 vs 92），直接拿来做均线会算错。要用需先做价格标定，暂不建议 |

核查工具（随时可重跑）：

```powershell
python tools\compare_tqqq_sources.py   # 周线口径对比
python tools\track_check.py            # 日线跟踪质量核查（更严谨）
```

---

## 九、重点时段盘中波动监控（防急跌）

### 为什么需要这个

趋势轨只看**周线状态变化**，太慢。

**真实案例**：2026-10-12 12:00 → 10-13 11:00 北京时间，
OKX TQQQ 永续从 **82.55 跌到 78.04（-5.46%）**，
而周线状态一直是 `bull`，趋势轨一声不吭。

所以系统加了盘中监控：**在你指定的重点时段内，用真实盘中K线盯急动**。

### 配置

```yaml
session_watch:
  enabled: true
  windows:                          # 北京时间
    - name: "美股全时段"
      start: "20:00"
      end: "04:00"                  # 覆盖盘前+开盘+盘中+收盘，跨零点
    - name: "午后监察"
      start: "14:30"
      end: "15:30"
  poll_interval_seconds: 60         # 时段内每 60 秒查一次（平时 300 秒）
  move_thresholds:                  # 最近 N 分钟涨跌幅超过 pct% 就告警
    - {window_minutes: 30, pct: 0.7}
    - {window_minutes: 60, pct: 1.0}
    - {window_minutes: 240, pct: 1.5}
  since_open_pct: 2.0               # 相对本时段开盘的累计涨跌幅
  cooldown_minutes: 30              # 冷却
  magnitude_bucket_pct: 1.0         # 幅度档位宽度
```

跨零点时段也支持（如 `20:00` - `04:00`），
还可以用 `weekdays: [0,1,2,3,4]` 限定只在工作日生效。

### 防刷屏：幅度分档 + 升级即报

**为什么必须做这个**：0.7% 的阈值配上 8 小时的长窗口，如果不加约束，
趋势行情里会每 5 分钟推你一条，把手机炸掉。

判定规则（`src/modules/session.py` 的 `decide_alert`，有单元测试覆盖）：

| 情况 | 行为 |
|---|---|
| 幅度**升级**到更高档位（如 -1% → -2%） | **立即推送**，无视冷却 |
| 档位**回落**（-2% → -1%，小幅反弹） | **不推**，不打扰 |
| 档位持平 + 冷却未过 | **不推** |
| 档位持平 + 冷却已过 | 推一次，提醒「还在持续」 |

档位宽度由 `magnitude_bucket_pct` 控制（默认 1.0 个百分点一档）。

### 实测：改完之后的真实效果

```powershell
python run.py --session-replay 26
```

回放最近 26 小时的真实 5 分钟K线，**含与线上完全相同的防刷屏逻辑**：

```
--- TQQQ-USDT-SWAP（313 根 5m K线）---
  10-12 21:30 北京 [美股全时段] [首次触发] 最大 1.19%  档位 1
  10-12 22:00 北京 [美股全时段] [冷却已过，仍在持续] 最大 1.22%  档位 1
  10-12 22:30 北京 [美股全时段] [冷却已过，仍在持续] 最大 1.68%  档位 1
  10-12 23:20 北京 [美股全时段] [冷却已过，仍在持续] 最大 1.56%  档位 1
  10-13 00:50 北京 [美股全时段] [冷却已过，仍在持续] 最大 1.54%  档位 1
  10-13 01:20 北京 [美股全时段] [冷却已过，仍在持续] 最大 1.59%  档位 1

--- CL-USDT-SWAP ---   3 条
--- ETH-USDT ---       1 条

合计本应触发 10 条告警
```

**26 小时 10 条 ≈ 每 2.6 小时 1 条**，不刷屏。
`21:30 北京` 正好是美股开盘（09:30 ET），当天开盘就往下砸。

觉得还是多？把 `cooldown_minutes` 调大（比如 60），或把 `magnitude_bucket_pct` 调大。

### 告警长什么样

微信通知栏只显示标题，所以标题也承载核心信息：`【TQQQ】盘中下跌 1.19%｜现价 80.23`

正文按「最重要 → 次要」排序：

```
【品种】TQQQ 纳指三倍做多（OKX 永续）（TQQQ-USDT-SWAP）
【发生了什么】下跌 1.19%　（30 分钟内下跌 1.19%（81.20 → 80.23），超过设定阈值 0.7%）
【时间】2026-10-13 21:30:12 北京时间 / 2026-10-13 13:30:12 UTC（来源 OKX ticker）
【当前位置】80.23
【压力位】82.61（上方 2.97%）
【支撑位】78.78（下方 1.81%）
【当下该做什么】建议人工查看/等待确认 —— 价格仍在支撑 78.78 与压力 82.61 之间，等它选方向
【信号类型】盘中波动
【AI解读】...
【重点时段】美股全时段 20:00-04:00
【你的持仓】多单 4x｜开仓价 100.0｜保证金 100.0U｜当前浮亏 12.80U（约 -12.80% 保证金收益）
【风险提示】以上为历史规律梳理，仅供参考，决策与风险自负
```

### 手动测试

```powershell
python run.py --once --session-only --ignore-window --dry-run   # 无视时段，演练不推送
python run.py --once --session-only --ignore-window             # 无视时段，真推送一条
```

---

## 十、本地可视化面板（只读 K线 + 历史告警标注）

### 它是什么

一个跑在本机的只读网页：左边当前状态、中间 K 线、右边历史消息。
它把**你微信里收到过的每一条告警**钉回到 K 线对应的那根上，
让你回头看清楚「当时是在什么位置提醒我的」。

**它同样是只读的**：服务端只实现了 `do_GET`，没有任何写操作接口；不下单、不推送、不调大模型。
界面上任何数字都来自真实 API，取不到的字段一律显示「数据源未提供」。

### 怎么启动

```powershell
python tools\dashboard.py                # 默认 127.0.0.1:8765，会自动打开浏览器
python tools\dashboard.py --port 8766    # 端口被占用时换一个
python tools\dashboard.py --no-browser   # 不自动开浏览器
```

启动后浏览器打开 `http://127.0.0.1:8765` 即可。
**它是手动启动的，不注册开机自启**，不想看就把窗口关掉。

> 端口被占用时程序会明确报错并告诉你怎么换端口，**不会偷偷改端口**——
> 否则你会以为面板还开着，实际它跑在另一个端口上。

### 面板上有什么

| 区域 | 内容 |
|---|---|
| 顶部 | 品种下拉（含已停用品种，停用的标「已停用」仍可查看）、自动刷新开关、立即刷新 |
| 左栏 | 现价、24h 高低、最近支撑/压力、周线均线、趋势状态 |
| 中间上方 | 行情条：大字现价（按涨跌着色）+ 24h 涨跌幅 + 24h 最高 / 最低 / 成交量 |
| 中间 | 图表左上角是周期切换（5m/15m/1H/4H/1D/1W），下面是 ECharts K 线（蜡烛 + 成交量副图，可滚轮缩放、可拖动） |
| 右栏 | 历史消息列表，可按类型筛选（趋势 / 事件预警 / 事件结果 / 做空观察位 / 盘中波动） |

**点右侧任意一条历史消息**，图会自动缩放并高亮到那根 K 线上，同时弹出该 K 线的开高低收。
如果那条消息的时间不在当前周期的可视范围内（比如 1D 只看最近几百天），
会提示你换个更长的周期或往前拖动。

### 图上的标注怎么看

| 标记 | 含义 |
|---|---|
| 蓝色 pin | 趋势轨告警 |
| 橙色 pin | 事件轨（公布前剧本 / 公布后结果），并额外用橙色色块标出所在时间段 |
| 红色 pin | 做空观察位 |
| 紫色 pin | 盘中波动告警 |
| **半透明 pin** | **日志回填**：系统上线前你已经在微信收到、但当时没落盘的历史消息，由日志补录 |
| 绿色虚线 | 最近支撑位（来自真实周K推导） |
| 红色虚线 | 最近压力位 |
| 细虚线 + 右端价签 | 现价线（现价取不到就整条不画；颜色跟随 24h 涨跌） |

### 数据从哪来

| 内容 | 来源 |
|---|---|
| 现价、K 线 | OKX 公开只读接口 `/api/v5/market/*`（取不到时显示「数据源未提供」，**不会拿旧值冒充**） |
| 历史消息 | `state/alerts.jsonl`（**只在推送成功后追加一条**，冷却/失败不写） |
| 日志回填 | 由 `logs/monitor-*.log` 里的历史推送记录补录 |

`state/` 已被 `.gitignore` 排除，**历史消息不会进公开仓库**。

### 从旧日志补录历史消息

系统刚上线时 `state/alerts.jsonl` 是空的，面板上就没有历史标注。可以扫日志补录：

```powershell
python tools\backfill_alerts.py            # 默认 dry-run，只统计不写盘
python tools\backfill_alerts.py --write    # 确认无误后再落盘
```

它会解析 `logs/monitor-*.log` 里的推送成功记录，兼容**新版 Markdown 正文**与
**旧版 `【字段】=值` 正文**两种格式，按 `(品种, 类型, 时间, 价格)` 去重后写入，
并打上 `log_backfill` 标记（面板上显示为半透明 pin）。
测试推送会被识别并跳过，解析不了的会**如实报告条数**而不是硬猜一个。

### 几个细节

- **K 线时间口径按北京时间对齐**：日K 开盘是北京时间 0 点，周K 开盘是北京时间周一 0 点。
  这两条都是用真实 OKX 样本实测核对过的，并有回归测试守着，不是想当然。
- **周期切换只影响图表**，右侧历史消息列表给的是全部历史，不会因为周期变短而丢。
- **自动刷新默认开启**（间隔由后端下发，默认 60 秒）；
  想边看边对着图分析就把它关掉，免得图画到一半被刷新打断。
- **界面配色对齐 OKX 暗色行情页**（绿涨 / 红跌），只是纯 CSS 的视觉近似，
  **没有使用 OKX 的任何 logo 或官方图片资源**。
- **图表库 ECharts 5.5 已本地化**到 `web/vendor/echarts.min.js`（Apache-2.0），
  页面不依赖 CDN，断网也能看。

---

## 十一、开机自启 / 停止 / 看状态

### 注册开机自启

```powershell
# 默认：登录后 60 秒启动（推荐，能用你个人级别的环境变量）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_autostart.ps1

# 可选：开机即启动、无需登录（此模式下 key 必须放在 .env 或 Machine 级环境变量）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_autostart.ps1 -RunAs system
```

注册后会有**两重保险**：

1. **Windows 任务计划程序** 里的任务 `DualTrackSignalMonitor`
   - 触发器：登录时 / 开机时
   - 设置：失败的自动重试、无执行时间上限（本来就该一直跑）
   - 任务本身挂了，Windows 会在 1 分钟后重新拉起来
2. **启动文件夹快捷方式**（仅 `-RunAs user` 模式）

### 崩溃自动重启

`scripts\run_forever.ps1` 是守护进程，它在崩溃时按退避策略重启：

```
退出 → 等 5s → 重启 → 崩 → 等 10s → 重启 → 崩 → 等 20s → ... 最多 60s
如果它稳定跑了超过 5 分钟，退避重置回 5s
```

**已实测验证**：直接 `Stop-Process -Force` 杀掉 Python 进程后，
守护脚本在 5 秒内拉起了新进程。

### 看状态

**最快的方法：双击项目根目录的 `check.bat`**，或执行：

```powershell
python tools\alive.py
```

它直接用大白话给你结论：

```
【结论】✓ 监控正在正常运行
        最后一次心跳：2026-10-14 19:47:27（1.0 分钟前）

--- 进程与计划任务 ---
计划任务 DualTrackSignalMonitor : Running
监控进程 (run.py --loop)       : pid 28728
守护进程 (run_forever.ps1)     : pid 3128
```

> **重点：判断「在不在跑」看的是【日志心跳】，不是进程在不在。**
> 进程可能被网络阻塞卡死，但心跳停了就是真的没在干活。
> 所以 `alive.py` 会告诉你「最后一次心跳是几分钟前」——
> 超过约 1.5 倍轮询间隔（平时 5 分钟、重点时段 1 分钟）就该警惕了。

它还会检查两种容易出事的重复启动：多个监控进程 / 多个守护进程
（重复会让你收到重复推送）。

更详细的信息用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\status.ps1
```

输出包括：任务状态、Python 进程、STOP 标志、key 是否就绪、
最近的日志尾部、当前各品种的趋势状态。

### 停止 / 卸载自启

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\unregister_autostart.ps1
```

它会写 `STOP_MONITOR.flag`（让守护脚本优雅退出）、删除计划任务和启动项、清理残留进程。
想重新开启就删掉 `STOP_MONITOR.flag` 再跑 `register_autostart.ps1`。

---

## 十二、日志在哪

全部在 `logs/` 目录，按天切分，UTF-8 编码：

| 文件 | 内容 |
|---|---|
| `logs/monitor-YYYY-MM-DD.log` | 主程序日志：拉数据、算趋势、生成剧本、推送结果 |
| `logs/supervisor-YYYY-MM-DD.log` | 守护进程日志：启动、崩溃、重启 |

查看：

```powershell
Get-Content "logs\monitor-$(Get-Date -Format 'yyyy-MM-dd').log" -Tail 50 -Encoding UTF8
```

> Windows PowerShell 5.1 默认按 ANSI 读文件，中文会乱码，
> **务必加 `-Encoding UTF8`**。

---

## 十三、命令速查

```powershell
python run.py --check                 # 自检：配置、数据源、key、红线
python tools\alive.py                 # 【最常用】它到底在不在跑？一行看结论
python run.py --once                  # 跑一轮后退出
python run.py --loop                  # 常驻循环（守护脚本内部用的就是它）
python run.py --test-push             # 只测微信推送链路
python run.py --list-events           # 打印未来经济日历（真实数据）
python run.py --dry-run --once --force  # 演练：不真推送，强制出信号
python run.py --once --trend-only     # 只跑趋势轨
python run.py --once --event-only     # 只跑事件轨
python run.py --once --symbol TQQQ-USDT-SWAP   # 只跑某个品种
python run.py --session-replay 26     # 回放最近 26 小时的重点时段告警（不推送）
python run.py --once --session-only --ignore-window --dry-run  # 盘中监控演练
python tools\dashboard.py             # 【面板】启动本地只读可视化面板（默认 8765 端口）
python tools\backfill_alerts.py       # 【面板】从旧日志补录历史告警（默认 dry-run，--write 才落盘）
python tests\test_safety.py           # 安全自测（224 项）
python tools\track_check.py           # 核查 OKX 永续 与 美股本土 的跟踪质量
python tools\compare_tqqq_sources.py  # 两套周线口径逐周对比
python tools\level_preview.py         # 预览各品种当前的支撑/压力位推导结果
```

---

## 十四、目录结构

```
TRADE/
├── run.py                      # 主程序入口（CLI）
├── config.yaml                 # 你的实际配置（install 时从模板生成）
├── config.example.yaml         # 配置模板（含全部参数注释）
├── .env                        # 你的 key（不会被提交）
├── .env.example                # key 模板
├── requirements.txt
├── README.md
├── src/
│   ├── config.py               # 配置加载 + key 读取（永不硬编码）
│   ├── logutil.py              # 日志落盘
│   ├── http_util.py            # 带重试的 HTTP 客户端
│   ├── state.py                # 状态记忆 + 推送去重冷却
│   ├── alert.py                # 告警格式渲染 + 风控措辞过滤 + 持仓盈亏
│   ├── datasource/
│   │   ├── okx.py              # OKX 只读行情（周K + 现价）
│   │   ├── us_stock.py         # 美股本土历史（Yahoo 主 / Stooq 备，补历史用）
│   │   └── econ_calendar.py    # AKShare 经济日历 + Polymarket（可选）
│   ├── modules/
│   │   ├── trend.py            # 【模块2】周线趋势轨
│   │   ├── event.py            # 【模块3】事件轨三分支
│   │   ├── session.py          # 【模块5】重点时段盘中波动监控
│   │   └── levels.py           # 支撑/压力位推导
│   ├── dashboard/              # 【模块6】本地只读可视化面板
│   │   ├── history.py          # 告警历史落盘/读取 + K线时间对齐
│   │   ├── service.py          # 面板数据聚合（带 TTL 缓存）
│   │   └── server.py           # 只读 HTTP 服务（只有 do_GET）
│   ├── llm/
│   │   ├── deepseek.py         # DeepSeek 客户端 + 防编造数字校验
│   │   └── prompts.py          # 固定 system prompt（命中缓存）+ 动态 user
│   └── notify/
│       └── serverchan.py       # 【模块4】Server酱 Turbo 推送
├── scripts/
│   ├── install.ps1             # 一键安装
│   ├── run_forever.ps1         # 崩溃自动重启守护
│   ├── register_autostart.ps1  # 注册开机自启
│   ├── unregister_autostart.ps1
│   └── status.ps1              # 状态检查
├── tests/
│   └── test_safety.py          # 红线自测
├── tools/
│   ├── compare_tqqq_sources.py # 周线口径对比工具
│   ├── track_check.py          # 跟踪质量核查工具
│   ├── level_preview.py        # 关键位推导预览
│   ├── dashboard.py            # 启动本地只读面板
│   └── backfill_alerts.py      # 从旧日志补录历史告警
├── web/                        # 面板前端（只读页面）
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   └── vendor/echarts.min.js   # 本地化的 ECharts，不依赖外网 CDN
├── logs/                       # 日志（会自动创建）
└── state/                      # 状态文件（趋势状态、去重记录、告警历史 alerts.jsonl）
```

---

## 十五、已知限制（重要，请务必读）

### 1. TQQQ-USDT-SWAP 自身历史很短（已解决）

该永续合约 **2026-06-28 才上线**，OKX 自己的周K只有 **15 根**，算不了 MA20/MA30。

**解决办法：已默认切到美股本土数据**（`history_source: "us_stock"`），
周K历史立刻变成 **261 根**，MA20/MA30 完全可用。
详见[第八节](#八历史数据来源用美股本土数据补历史)。

如果你把 `history_source` 改回 `"okx"`，系统会重新显示「MA20=数据不足」——
**绝不会用不足的数据硬算一个均线给你看。**

### 2. 经济日历的"预期值"经常缺失

AKShare 的百度经济日历对很多事件不提供预期值（实测约一半左右）。
缺的时候系统会：

- 在告警里显示「市场预期：数据源未提供预期值」
- 把剧本口径从「实际 vs 预期」切换成「实际 vs 前值」，并明确提示强度下降
- **绝不自己编一个预期值**

### 3. 重要性字段可能偏低

AKShare 会把美国 CPI 这类关键数据标成"中"而不是"高"。
所以系统用的是 **重要性 + 地区 + 关键词** 三重过滤，而不是只看重要性。

实测：未来 7 天 457 条日历 → 过滤后剩余 14 条关注事件。

### 4. 事件轨单轮推送有条数上限

`event.max_events_per_run`（默认 3）防止某天事件扎堆时连环轰炸。

### 5. 大模型不可用时自动降级

没有 `DEEPSEEK_API_KEY`、网络不通、或输出被防编造校验拦截时，
系统会退回**本地规则模板**生成解读，并在文案里注明「本条解读由本地规则模板生成」。
数字依然全部来自真实数据，功能不中断。

---

## 十六、常见问题

**Q：会不会偷偷下单？**
不会。代码里没有任何下单/平仓/撤单调用，也不引用任何券商 SDK。
只调用 OKX 的 `/api/v5/market/*`（公开行情）和 `/api/v5/public/*`（交易对信息）这类只读端点。
你可以用 `python run.py --check` 的「红线自查」章节确认。

**Q：为什么同一信号不反复提醒我？**
`notify.cooldown_minutes`（默认 240 分钟）做去重冷却。
趋势轨还额外要求**状态发生变化**才推送——状态没变就不出声。

**Q：为什么 `curl` 用不了，但程序能联网？**
这台机器上有软件按进程拦截了 `curl.exe`（实测连接超时，退出码 28），
但 PowerShell 和 Python 的网络请求正常。程序内部用的是 Python `requests`，不受影响。

**Q：想调整消息刷屏的频率？**
- 少推趋势：调大 `notify.cooldown_minutes`
- 少推事件：调小 `event.pre_alert_minutes`，或调小 `event.max_events_per_run`
- 完全静音某个品种：把它改成 `enabled: false`

**Q：默认模型是什么？怎么改？**
默认 `deepseek-chat`。改 `config.yaml` 里的 `llm.model` 即可。
成本优化已内置：不变的规则全在固定的 system prompt 里（命中 DeepSeek 上下文缓存），
每次变化的行情数据才放进 user message。

---

## 十七、完整流程文档与部署提示词

两份配套文档：

| 文档 | 内容 |
|---|---|
| [FLOWS.md](FLOWS.md) | **系统全流程**：架构、主循环、五个模块的算法细节、状态流转、推送格式、部署守护机制、**13 个已踩过的坑及修法**、测试覆盖、文件索引 |
| [DEPLOY_PROMPT.md](DEPLOY_PROMPT.md) | **在另一台机器上复现本方案的提示词**。整段复制粘贴给那台机器的 agent 即可；含红线、必须确认的信息、验收标准、已知坑，以及「直接迁移现有部署」的最快路径 |

想快速了解系统怎么运转，看 FLOWS.md；
想换台机器重装或在 Mac/Linux 上部署，用 DEPLOY_PROMPT.md。

---

## 免责声明

本系统输出的所有内容均为**历史规律的机械梳理**，不构成任何投资建议。

**以上为历史规律梳理，仅供参考，决策与风险自负。**
