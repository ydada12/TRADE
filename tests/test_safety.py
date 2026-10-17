"""安全自测：证明红线条款在代码层面真的生效。

不依赖 pytest，直接运行：
    python tests/test_safety.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alert import SIGNAL_SHORT, SIGNAL_WORLD, SafetyFilter, compute_position_pnl, fmt_num
from src.datasource.okx import BookLevel, Candle, OrderBook, Ticker, Trade
from src.llm.deepseek import find_fabricated_numbers
from src.modules.event import determine_branch, render_fallback_script
from src.modules.levels import derive_intraday_levels
from src.modules.short_signal import (
    OrderflowMetrics,
    compute_orderflow_metrics,
    score_short_signal,
)
from src.modules.trend import evaluate_trend

BJ = timezone(timedelta(hours=8))

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


# ---------------------------------------------------------------- 红线2：防编造数字
def test_number_guard() -> None:
    print("\n[红线2] 大模型不许编造数字")
    facts = {
        "周线收盘价": 81.69,
        "实时最新价": 81.01,
        "均线": {"MA10": 75.195, "MA20": None},
        "支撑位": 79.51,
        "压力位": 81.5,
    }

    ok_text = "周线收盘 81.69，现价 81.01，MA10 为 75.195，下方支撑 79.51，上方压力 81.5。"
    check("引用真实数字 -> 通过", find_fabricated_numbers(ok_text, facts) == [],
          str(find_fabricated_numbers(ok_text, facts)))

    bad_text = "周线收盘 81.69，但如果你看 88.88 这个位置会更关键。"
    bad = find_fabricated_numbers(bad_text, facts)
    check("凭空出现 88.88 -> 拦截", "88.88" in bad, str(bad))

    bad2 = "预期值大约 5.2，实际值可能 5.5。"
    bad2_hits = find_fabricated_numbers(bad2, facts)
    check("凭空编造经济数据 5.2/5.5 -> 拦截",
          "5.2" in bad2_hits and "5.5" in bad2_hits, str(bad2_hits))

    # 结构性用词（剧本A/B/C、三分支）不应被误判
    structural = "给出 3 个分支，剧本A 与剧本B 各 2 句话。"
    check("结构性数字不误杀", find_fabricated_numbers(structural, facts) == [],
          str(find_fabricated_numbers(structural, facts)))

    # 字段名里带数字（均线 MA20/MA30）必须算合法，否则「MA20=74.4」整段解读被误拦
    with_ma = {"均线": {"MA20": 74.426, "MA30": 69.293}, "现价": 74.09}
    ma_text = "现价 74.09 贴着 MA20（74.426），MA30（69.293）仍在下方。"
    check("MA20/MA30 这类字段名里的数字不误杀",
          find_fabricated_numbers(ma_text, with_ma) == [],
          str(find_fabricated_numbers(ma_text, with_ma)))
    check("顺带编造的 88.8 仍然被拦",
          "88.8" in find_fabricated_numbers(ma_text + " 另外 88.8 也很关键。", with_ma),
          str(find_fabricated_numbers(ma_text + " 另外 88.8 也很关键。", with_ma)))


# ---------------------------------------------------------------- 红线1：禁用买卖措辞
def test_forbidden_words() -> None:
    print("\n[红线1] 严禁买卖建议措辞")
    f = SafetyFilter(
        forbidden_words=["建议买入", "建议卖出", "加仓", "抄底", "必涨", "必跌"],
        fallback_text="",
        allowed_action_text="建议人工查看/等待确认",
        disclaimer="以上为历史规律梳理，仅供参考，决策与风险自负",
    )
    safe, blocked = f.sanitize_ai_text("当前价格站上均线，可以观察。", "fallback")
    check("正常文案通过", safe == "当前价格站上均线，可以观察。" and not blocked)

    for bad in ["建议买入 该品种", "现在可以加仓", "这波必涨", "建议卖出止盈"]:
        text, was_blocked = f.sanitize_ai_text(bad, "已替换为安全模板")
        check(f"拦截「{bad}」", was_blocked and text == "已替换为安全模板", text)

    check("建议动作被强制固定", f.enforce_action("建议买入") == "建议人工查看/等待确认")


# ---------------------------------------------------------------- 红线3：风险提示
def test_disclaimer() -> None:
    print("\n[红线3] 每条输出必须带风险提示")
    f = SafetyFilter(
        forbidden_words=[], fallback_text="",
        allowed_action_text="建议人工查看/等待确认",
        disclaimer="以上为历史规律梳理，仅供参考，决策与风险自负",
    )
    out = f.ensure_disclaimer("【信号类型】趋势\n【品种】X")
    check("缺失时自动补齐", "以上为历史规律梳理，仅供参考，决策与风险自负" in out)

    out2 = f.ensure_disclaimer(out)
    check("已有时不重复追加", out2.count("以上为历史规律梳理") == 1)


# ---------------------------------------------------------------- 趋势轨：数据不足
def test_trend_insufficient_data() -> None:
    print("\n[趋势轨] 数据不足必须如实报告，不许伪造均线")
    candles = [
        Candle(ts_ms=1_700_000_000_000 + i * 604_800_000, open=10.0, high=11.0,
               low=9.0, close=10.0 + i * 0.1, volume=1.0, confirmed=True)
        for i in range(15)
    ]
    snap = evaluate_trend(
        inst_id="TEST", name="测试品种", candles=candles, last_price=12.0,
        ma_periods=[10, 20], cross_pairs=[[10, 20]], prev_state=None,
    )
    check("MA10 可算", snap.ma_values[10] is not None, str(snap.ma_values))
    check("MA20 被判为不可用（None）", snap.ma_values[20] is None, str(snap.ma_values))
    check("给出了数据不足说明", any("MA20" in s for s in snap.insufficient), str(snap.insufficient))


# ---------------------------------------------------------------- 事件轨：三分支判定
def test_branch_determination() -> None:
    print("\n[事件轨] 三分支判定与口径")
    check("实际>预期 -> A", determine_branch(3.9, 3.7, 3.4)[0] == "A")
    check("实际<预期 -> B", determine_branch(3.5, 3.7, 3.4)[0] == "B")
    check("实际=预期 -> C", determine_branch(3.7, 3.7, 3.4)[0] == "C")
    check("无预期值 -> 用前值口径 A", determine_branch(5.0, None, 4.0)[0] == "A")
    check("无预期值 -> 用前值口径 B", determine_branch(3.0, None, 4.0)[0] == "B")
    check("全都缺 -> unknown", determine_branch(3.0, None, None)[0] == "unknown")

    facts = {
        "事件名称": "美国9月CPI年率未季调(%)",
        "地区": "美国",
        "市场一致预期值": "数据源未提供预期值",
        "支撑位": 79.51,
        "压力位": 81.5,
    }
    script = render_fallback_script(facts, "TQQQ-USDT-SWAP", region="美国")
    check("预期缺失时口径切换为「前值」", "前值" in script and "数据源未提供市场一致预期值" in script)
    check("预期缺失时不写「高于预期」", "实际 > 预期" not in script)

    facts_us = dict(facts, **{"市场一致预期值": "3.7"})
    script_us = render_fallback_script(facts_us, "TQQQ-USDT-SWAP", region="美国")
    check("美国事件用「美联储」措辞", "美联储" in script_us)

    facts_cn = dict(facts_us, **{"事件名称": "中国9月CPI年率(%)"})
    script_cn = render_fallback_script(facts_cn, "TQQQ-USDT-SWAP", region="中国")
    check("非美事件不套「美联储」", "美联储" not in script_cn, script_cn[:80])

    script_oil = render_fallback_script(
        dict(facts_us, **{"事件名称": "美国EIA原油库存变动(万桶)"}), "CL-USDT-SWAP", region="美国")
    check("原油库存用「油价」措辞", "油价" in script_oil)


# ---------------------------------------------------------------- 持仓盈亏计算
def test_position_pnl() -> None:
    print("\n[持仓] 浮盈亏为本地确定性计算")
    pnl = compute_position_pnl(
        {"side": "long", "entry_price": 100.0, "margin_usdt": 100.0, "leverage": 4},
        last_price=96.8,
    )
    check("能算出盈亏", pnl is not None)
    if pnl:
        check("名义价值 = 保证金 x 杠杆", abs(pnl.notional - 400.0) < 1e-6, str(pnl.notional))
        check("浮亏为负", pnl.unrealized < 0, str(pnl.unrealized))
        check("盈亏符号与方向一致（多单跌则亏）",
              abs(pnl.unrealized - (96.8 - 100.0) * pnl.qty) < 1e-6)

    short = compute_position_pnl(
        {"side": "short", "entry_price": 100.0, "margin_usdt": 10.0, "leverage": 2},
        last_price=90.0,
    )
    check("空单下跌为盈", short is not None and short.unrealized > 0, str(short))
    check("缺数据返回 None",
          compute_position_pnl({"side": "long", "entry_price": None}, 1.0) is None)


# ---------------------------------------------------------------- 其它
def test_formatting() -> None:
    print("\n[格式] 数字呈现")
    check("None -> 如实说明", fmt_num(None) == "数据源未提供")
    check("大数带千分位", fmt_num(1234.5) == "1,234.5", fmt_num(1234.5))
    check("大数保留两位", fmt_num(2550.98) == "2,550.98", fmt_num(2550.98))
    check("小数去尾零", fmt_num(81.5000) == "81.5", fmt_num(81.5000))
    check("小币价保留精度", fmt_num(1.2443300) == "1.24433", fmt_num(1.2443300))


# ---------------------------------------------------------------- 关键位推导
def test_levels() -> None:
    print("\n[关键位] 只能来自真实K线，且不能紧贴现价")
    from src.modules.levels import derive_levels, detect_level_breaks

    candles = [
        Candle(ts_ms=1_700_000_000_000 + i * 604_800_000,
               open=100.0 + i, high=105.0 + i, low=95.0 + i, close=102.0 + i,
               volume=1.0, confirmed=True)
        for i in range(20)
    ]
    price = 120.0
    lv = derive_levels("TEST", candles, price, lookback_weeks=12, min_distance_pct=1.5)

    check("支撑位全部低于现价", all(s < price for s in lv.supports), str(lv.supports[:3]))
    check("压力位全部高于现价", all(r > price for r in lv.resistances), str(lv.resistances[:3]))
    check("最近支撑有实际距离（>1.5%）",
          lv.nearest_support() is None or (price - lv.nearest_support()) / price > 0.015,
          str(lv.nearest_support()))

    # 手工指定的位置必须被采纳（尊重用户判断，不做距离过滤）
    lv2 = derive_levels("TEST", candles, price, lookback_weeks=12,
                        manual_support=[119.0], manual_resistance=[121.0],
                        min_distance_pct=1.5)
    check("手动支撑位被保留", 119.0 in lv2.supports, str(lv2.supports))
    check("手动压力位被保留", 121.0 in lv2.resistances, str(lv2.resistances))
    check("来源标注为 mixed/manual", lv2.source in {"mixed", "manual"}, lv2.source)

    # 破位判定
    breaks = detect_level_breaks(
        derive_levels("TEST", candles, 120.0, lookback_weeks=12,
                      manual_support=[119.0], manual_resistance=[121.0]),
        current_price=118.0, prev_close=120.0, tolerance_pct=0.3,
    )
    check("跌破手动支撑会被识别", any(b["kind"] == "break_support" for b in breaks), str(breaks))


# ---------------------------------------------------------------- 触发条件措辞
def test_trigger_wording() -> None:
    print("\n[措辞] 状态没变化时不许写「由X变为X」")
    # 构造一段稳定上涨的周K：状态应为 bull
    candles = [
        Candle(ts_ms=1_700_000_000_000 + i * 604_800_000, open=10.0 + i,
               high=11.0 + i, low=9.0 + i, close=10.0 + i, volume=1.0, confirmed=True)
        for i in range(40)
    ]

    # 上一次状态就是 bull -> 本轮状态没变 -> 不应产生 state_change 信号
    snap_same = evaluate_trend(
        inst_id="TEST", name="测试", candles=candles, last_price=50.0,
        ma_periods=[20, 30], cross_pairs=[[20, 30]], prev_state="bull",
    )
    check("状态一致时判定为 bull", snap_same.state == "bull", snap_same.state)
    check("状态一致时不产生 state_change 信号",
          not any(s.kind == "state_change" for s in snap_same.signals),
          str([s.kind for s in snap_same.signals]))
    check("state_changed 为 False", snap_same.state_changed is False)

    # 上一次是 range -> 本轮转 bull -> 应产生 state_change
    snap_diff = evaluate_trend(
        inst_id="TEST", name="测试", candles=candles, last_price=50.0,
        ma_periods=[20, 30], cross_pairs=[[20, 30]], prev_state="range",
    )
    check("状态变化时产生 state_change 信号",
          any(s.kind == "state_change" for s in snap_diff.signals),
          str([s.kind for s in snap_diff.signals]))
    check("state_changed 为 True", snap_diff.state_changed is True)


# ---------------------------------------------------------------- 重点时段盘中监控
def test_session_watch() -> None:
    print("\n[盘中监控] 时段判定与波动计算")
    from datetime import datetime, timedelta, timezone
    from src.modules.session import (
        UTC8, TimeWindow, active_window, detect_moves, parse_windows,
    )

    windows = parse_windows([
        {"name": "美股盘前+开盘", "start": "20:00", "end": "22:30"},
        {"name": "午后监察", "start": "14:30", "end": "15:30"},
        {"name": "跨零点", "start": "23:00", "end": "01:00"},
    ])
    check("解析出 3 个时段", len(windows) == 3, str(len(windows)))

    def at(hh, mm):
        return datetime(2026, 10, 13, hh, mm, tzinfo=UTC8)

    check("21:00 落在开盘时段", active_window(windows, at(21, 0)).name == "美股盘前+开盘")
    check("14:45 落在午后时段", active_window(windows, at(14, 45)).name == "午后监察")
    check("12:00 不在任何时段", active_window(windows, at(12, 0)) is None)
    check("00:30 落在跨零点时段", active_window(windows, at(0, 30)).name == "跨零点")
    check("19:59 不在开盘时段", active_window(windows, at(19, 59)) is None)
    check("22:31 不在开盘时段", active_window(windows, at(22, 31)) is None)

    # 跨零点窗口的「开启时刻」应回溯到前一天
    start = windows[2].start_moment(at(0, 30))
    check("跨零点时段起点回溯到前一天 23:00",
          start.day == 12 and start.hour == 23, str(start))

    # 波动计算：构造 5 分钟 K 线，每根跌 0.5
    #   -> 30 分钟（6 根）= 约 3.4%   150 分钟（30 根）= 15%
    base = 1_760_000_000_000
    candles = [
        Candle(ts_ms=base + i * 300_000, open=100 - i * 0.5, high=100 - i * 0.5,
               low=100 - i * 0.5, close=100 - i * 0.5, volume=1.0, confirmed=True)
        for i in range(31)
    ]
    now_ms = candles[-1].ts_ms
    hits = detect_moves(candles, now_ms,
                        rules=[{"window_minutes": 30, "pct": 1.0},
                               {"window_minutes": 150, "pct": 50.0}],
                        since_open_ms=base, since_open_pct=2.0)
    kinds = {(h["kind"], h.get("minutes")) for h in hits}
    check("30分钟跌约3.4% 触发", ("window_move", 30) in kinds, str(kinds))
    check("阈值 50% 的规则不触发", ("window_move", 150) not in kinds, str(kinds))
    check("相对时段开盘跌 15% 触发", ("since_open", None) in kinds, str(kinds))
    thirty = next(h for h in hits if h.get("minutes") == 30)
    check("30分钟涨跌幅约为 -3.4%", -3.6 < thirty["change_pct"] < -3.2,
          str(thirty["change_pct"]))
    biggest = max(hits, key=lambda h: abs(h["change_pct"]))
    check("方向判定为下跌", biggest["direction"] == "下跌", biggest["direction"])
    check("涨跌幅符号为负", biggest["change_pct"] < 0, str(biggest["change_pct"]))

    flat = [Candle(ts_ms=base + i * 300_000, open=100, high=100, low=100,
                   close=100, volume=1.0, confirmed=True) for i in range(31)]
    check("横盘不触发任何告警",
          detect_moves(flat, flat[-1].ts_ms,
                       rules=[{"window_minutes": 30, "pct": 1.0}],
                       since_open_ms=base, since_open_pct=2.0) == [])

    # ---- 防刷屏判定 ----
    from src.modules.session import decide_alert, magnitude_bucket

    check("档位映射：0.9% -> 0 档", magnitude_bucket(0.9, 1.0) == 0)
    check("档位映射：1.4% -> 1 档", magnitude_bucket(1.4, 1.0) == 1)
    check("档位映射：2.1% -> 2 档", magnitude_bucket(2.1, 1.0) == 2)
    check("档位映射：涨跌同档", magnitude_bucket(-1.4, 1.0) == 1)

    now = 1_800_000_000.0
    d = decide_alert(1.2, None, -1, now, 30.0, 1.0)
    check("首次触发 -> 告警", d.alert and d.reason == "首次触发", d.reason)

    d = decide_alert(2.3, now - 60, 1, now, 30.0, 1.0)
    check("档位升级 -> 冷却中也立即告警", d.alert and "升级" in d.reason, d.reason)

    d = decide_alert(1.4, now - 60, 2, now, 30.0, 1.0)
    check("档位回落 -> 不告警（小幅反弹不打扰）", not d.alert and "回落" in d.reason, d.reason)

    d = decide_alert(1.4, now - 60, 1, now, 30.0, 1.0)
    check("档位持平且冷却中 -> 不告警", not d.alert and "冷却" in d.reason, d.reason)

    d = decide_alert(1.4, now - 3600, 1, now, 30.0, 1.0)
    check("档位持平但冷却已过 -> 告警（提醒仍在持续）", d.alert, d.reason)

    # 单位陷阱：冷却用秒比较。若 now_ts 和 last_ts 都用毫秒，同样 5 分钟的间隔
    # 会被误判成「冷却已过」，导致几乎每根K线都告警（回放实测 10 条变 27 条）。
    ok_sec = decide_alert(1.2, now - 300, 1, now, 30.0, 1.0)
    bad_ms = decide_alert(1.2, now * 1000 - 300_000, 1, now * 1000, 30.0, 1.0)
    check("秒单位：5 分钟前仍在 30 分冷却内 -> 不告警", not ok_sec.alert, ok_sec.reason)
    check("毫秒单位：同样间隔被误判为冷却已过 -> 错误告警",
          bad_ms.alert, bad_ms.reason)


# ---------------------------------------------------------------- 推送格式
def test_alert_layout() -> None:
    print("\n[格式] 最重要的信息在最前面，且顺序固定")
    from src.alert import Alert, short_symbol, zone_hint

    a = Alert(
        symbol="TQQQ 纳指三倍做多（OKX 永续）（TQQQ-USDT-SWAP）",
        short_name=short_symbol("TQQQ-USDT-SWAP"),
        what_happened="30 分钟内下跌 1.19%",
        time_text="2026-10-13 21:30:00 北京时间",
        price=80.23,
        support=78.78,
        resistance=82.61,
        action_text="建议人工查看/等待确认 —— 价格仍在区间内",
        ai_text="历史上这类波动通常需要观察能否站稳。",
        signal_type="盘中波动",
        disclaimer="以上为历史规律梳理，仅供参考，决策与风险自负",
    )
    text = a.render()
    lines = [ln for ln in text.splitlines() if ln.strip()]

    check("短名从 inst_id 提取", a.short_name == "TQQQ", a.short_name)
    check("段间用空行分隔（Server酱³ 换行要求）", "\n\n" in text, text)
    check("首行是二级标题且含品种短名",
          lines[0].startswith("## ") and "TQQQ" in lines[0], lines[0])
    check("标题行带信号类型", "盘中波动" in lines[0], lines[0])
    check("发生了什么用粗体突出", lines[1] == "**30 分钟内下跌 1.19%**", lines[1])
    check("现价带 emoji 与粗体标签", "📊 **现价** 80.23" in text, text)
    check("压力位标注在上方且距离正确",
          "🔴 **压力位** 82.61（上方 2.97%）" in text, text)
    check("支撑位标注在下方且距离正确",
          "🟢 **支撑位** 78.78（下方 1.81%）" in text, text)
    check("时间行以时钟 emoji 开头", any(ln.startswith("🕒 ") for ln in lines), lines)
    check("当下该做什么是三级标题", "### ⚠️ 当下该做什么" in lines, lines)
    check("建议动作粗体单独成段", "**建议人工查看/等待确认" in text, text)
    check("AI 思考是三级标题", "### 🤖 AI 思考" in text, text)
    check("AI 思考段落排在「当下该做什么」之后",
          text.index("### ⚠️ 当下该做什么") < text.index("### 🤖 AI 思考"), text)
    check("风险提示在最后一行且用引用块弱化",
          lines[-1].startswith("> 【风险提示】") and a.disclaimer in lines[-1], lines[-1])
    check("正文不再出现旧式【标签】行首",
          not any(ln.startswith("【品种】") or ln.startswith("【发生了什么】") for ln in lines),
          lines)

    # 标题（微信通知栏只显示标题，必须自带品种+发生了什么+现价）
    title = a.title()
    check("标题含品种短名", "TQQQ" in title, title)
    check("标题含发生了什么", "1.19%" in title, title)
    check("标题含现价", "80.23" in title, title)

    # 当下该做什么：只能给客观位置，不许给方向
    check("跌破支撑时只描述客观状态",
          "已跌破支撑位" in zone_hint(70.0, 78.78, 82.61), zone_hint(70.0, 78.78, 82.61))
    check("上破压力时只描述客观状态",
          "已上破压力位" in zone_hint(90.0, 78.78, 82.61), zone_hint(90.0, 78.78, 82.61))
    check("区间内提示等方向",
          "等它选方向" in zone_hint(80.0, 78.78, 82.61), zone_hint(80.0, 78.78, 82.61))

    for word in ["买入", "卖出", "加仓", "抄底", "必涨", "必跌"]:
        check(f"当下该做什么不含「{word}」",
              word not in a.action_text and word not in zone_hint(70.0, 78.78, 82.61))

    # 缺关键位时如实说明，不编数字
    b = Alert(symbol="X", short_name="X", what_happened="y", time_text="z",
              price=10.0, support=None, resistance=None, action_text="建议人工查看/等待确认",
              ai_text="", signal_type="盘中波动",
              disclaimer="以上为历史规律梳理，仅供参考，决策与风险自负")
    check("无关键位时如实标注", "数据源未提供" in b.render(), b.render())

    # 大模型不可用时：解读段落改叫「规则文案」，不能让模板冒充 AI 写的思考
    c = Alert(symbol="X", short_name="X", what_happened="y", time_text="z",
              price=10.0, action_text="建议人工查看/等待确认",
              ai_text="规则模板文案（说明：本条解读由本地规则模板生成）",
              rule_text=True, signal_type="趋势",
              disclaimer="以上为历史规律梳理，仅供参考，决策与风险自负")
    c_text = c.render()
    check("兜底解读标注为规则文案", "### 📝 规则文案" in c_text, c_text)
    check("兜底解读不再出现「AI 思考」小标题", "AI 思考" not in c_text, c_text)

    # 事件轨同样要有一段思考（公布前剧本 / 公布后对号入座）
    from src.alert import EventAlert

    ev = EventAlert(
        headline="美国9月CPI年率未季调(%)", stage="post",
        happened="实际值 3.9 高于预期 3.7", time_text="2026-10-14 20:30 北京时间",
        body="现在的状况：CPI 强于预期\n我怎么想：偏空\n我在意的：看 82.61 能否站稳",
        short_name="TQQQ", price=81.2, action_text="建议人工查看/等待确认",
        disclaimer="以上为历史规律梳理，仅供参考，决策与风险自负",
    )
    ev_text = ev.render()
    check("事件轨思考段落是三级小标题且排在正文后",
          "### 🤖 AI 思考" in ev_text
          and ev_text.index("### 🤖 AI 思考") > ev_text.index("**实际值"),
          ev_text)
    check("事件轨思考含三段式标签",
          "现在的状况" in ev_text and "我怎么想" in ev_text and "我在意的" in ev_text,
          ev_text)


# ---------------------------------------------------------------- 回归：盘中档位跨天重置
def test_session_window_reset() -> None:
    print("\n[回归] 盘中防刷屏记录按「时段的某一次开启」重置")
    from src.modules.session import decide_alert, record_for_window

    day1 = 1_800_000_000
    day2 = day1 + 86400
    rec = {"last_ts": day1 + 3600, "last_bucket": 3, "window_start_ts": day1}

    check("同一次时段内记录保留", record_for_window(rec, day1) is rec)
    check("换了一天（时段重新开启）记录清零", record_for_window(rec, day2) == {})
    legacy = {"last_ts": day1 + 3600, "last_bucket": 3}          # 修复前写入的旧记录
    check("没有 window_start_ts 的旧记录视为过期", record_for_window(legacy, day2) == {})
    check("空记录返回空字典", record_for_window(None, day1) == {})
    manual = {"last_ts": 1.0, "last_bucket": 1, "window_start_ts": None}
    check("手动不限时段(None)的记录在手动模式下保留", record_for_window(manual, None) is manual)

    # 复现原 bug：昨天 3 档，今天 -1.3% 被「幅度回落」吞掉
    stale = decide_alert(1.3, rec["last_ts"], rec["last_bucket"], day2 + 3600, 30.0, 1.0)
    check("（旧行为）昨天3档会压住今天1档 -> 被吞", not stale.alert, stale.reason)
    fixed_rec = record_for_window(rec, day2)
    fixed = decide_alert(1.3, fixed_rec.get("last_ts"), int(fixed_rec.get("last_bucket", -1)),
                         day2 + 3600, 30.0, 1.0)
    check("（修复后）今天重新开始计档位 -> 首次触发告警", fixed.alert and "首次" in fixed.reason,
          fixed.reason)


# ---------------------------------------------------------------- 回归：推送失败不丢信号
def _make_monitor(tmp_dir: Path):
    """用示例配置造一个 Monitor，状态/日志都落在临时目录，不碰真实 state。"""
    import logging

    from run import Monitor
    from src.config import load_config

    cfg = load_config()
    cfg._data["app"]["state_file"] = str(tmp_dir / "state.json")
    cfg._data["app"]["log_dir"] = str(tmp_dir / "logs")
    # 告警历史也要落到临时目录：否则测试造出来的假告警会混进真实的 state/alerts.jsonl，
    # 面板上就会多出「测试时才存在」的点位。
    cfg._data["app"]["alert_history_file"] = str(tmp_dir / "alerts.jsonl")
    # 控制项与延迟队列同样落到临时目录：测试不碰真实的 state/control.json
    cfg._data["app"]["control_file"] = str(tmp_dir / "control.json")
    cfg._data["app"]["pending_file"] = str(tmp_dir / "pending.json")
    monitor = Monitor(cfg, dry_run=False)
    # Windows 下未释放的日志文件句柄会让 TemporaryDirectory 清理失败（WinError 32）。
    # 测试不校验日志文件内容，这里关掉文件 handler，只留控制台输出。
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)
            handler.close()
    monitor.llm.translate = lambda sp, um, facts, fallback: (fallback, "template")
    return monitor


def test_push_failure_not_lost() -> None:
    print("\n[回归] 推送失败时不落状态，下一轮重试；成功后才记已发")
    import tempfile
    import time
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from run import MAX_PUSH_RETRIES
    from src.datasource.econ_calendar import BEIJING, EconEvent
    from src.datasource.okx import Ticker
    from src.modules.levels import Levels

    levels_dict = Levels("TQQQ-USDT-SWAP", 80.0, supports=[78.0], resistances=[82.0]).as_dict()

    # ---------- 事件轨 ----------
    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))
        pub = datetime.now(BEIJING) + timedelta(minutes=60)
        event = EconEvent(date=pub.strftime("%Y-%m-%d"), time=pub.strftime("%H:%M"),
                          region="美国", name="美国9月CPI年率", forecast=3.7,
                          previous=3.5, importance=3)
        fp = f"event|{event.fingerprint}"
        m.calendar.fetch_range = lambda *a, **k: [event]
        m._symbol_levels = lambda symbol: (levels_dict, 80.0, "2026-10-14 00:00:00", 1760000000000)

        sent: list[str] = []
        net = {"down": True}

        def fake_send(title, body):
            sent.append(title)
            return SimpleNamespace(ok=not net["down"], detail="boom")

        m.notifier.send = fake_send

        m.run_events()
        rec = m.state.get_event(fp) or {}
        check("事件预警推送失败 -> 不标记 pre_sent", not rec.get("pre_sent"), str(rec))
        m.run_events()
        check("失败后下一轮会重试（又尝试发送了一次）", len(sent) == 2, str(len(sent)))

        net["down"] = False
        m.run_events()
        rec = m.state.get_event(fp) or {}
        check("网络恢复后补发成功 -> 标记 pre_sent", rec.get("pre_sent") is True, str(rec))
        n_after = len(sent)
        m.run_events()
        check("已发过不再重复发", len(sent) == n_after, f"{len(sent)} vs {n_after}")

    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))
        m.calendar.fetch_range = lambda *a, **k: [event]
        m._symbol_levels = lambda symbol: (levels_dict, 80.0, "ts", 1760000000000)
        sent2: list[str] = []
        m.notifier.send = lambda t, b: (sent2.append(t),
                                        SimpleNamespace(ok=False, detail="down"))[1]
        for _ in range(MAX_PUSH_RETRIES + 3):
            m.run_events()
        check(f"长期故障最多尝试 {MAX_PUSH_RETRIES} 次后放弃，不无限重试",
              len(sent2) == MAX_PUSH_RETRIES, str(len(sent2)))

    # ---------- 趋势轨 ----------
    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))
        week = 7 * 86400 * 1000
        base = 1_700_000_000_000
        candles = [Candle(ts_ms=base + i * week, open=100 + i, high=101 + i, low=99 + i,
                          close=100 + i, volume=1.0, confirmed=True) for i in range(30)]
        inst = "TQQQ-USDT-SWAP"
        m.okx.get_instrument = lambda *a, **k: {"instId": inst}
        m.okx.get_ticker = lambda i: Ticker(i, 131.0, 0, 0, 0, 0, int(time.time() * 1000))
        m._weekly_candles = lambda symbol: (candles, "测试数据")
        m.state.set_trend_state(inst, {"state": "range"})

        sent3: list[str] = []
        net3 = {"down": True}
        m.notifier.send = lambda t, b: (sent3.append(t),
                                        SimpleNamespace(ok=not net3["down"], detail="x"))[1]

        m.run_trend(only=inst)
        check("趋势切换推送失败 -> 状态仍停在旧状态(range)",
              (m.state.get_trend_state(inst) or {}).get("state") == "range",
              str(m.state.get_trend_state(inst)))
        net3["down"] = False
        m.run_trend(only=inst)
        check("恢复后重试成功 -> 状态更新为 bull 并已推送",
              (m.state.get_trend_state(inst) or {}).get("state") == "bull" and len(sent3) == 2,
              f"{m.state.get_trend_state(inst)} sent={len(sent3)}")
        m.run_trend(only=inst)
        check("状态不再变化 -> 不重复推送", len(sent3) == 2, str(len(sent3)))

    # ---------- 重试计数持久化与清理 ----------
    from src.state import StateStore
    with tempfile.TemporaryDirectory() as td:
        store = StateStore(Path(td) / "s.json")
        check("重试计数从 1 开始累加", store.bump_retry("k") == 1 and store.bump_retry("k") == 2)
        store.clear_retry("k")
        check("清除后重新计数", store.bump_retry("k") == 1)
        store.save()
        again = StateStore(Path(td) / "s.json")
        check("重试计数能落盘并读回", again.bump_retry("k") == 2)
        store._data["retries"]["old"] = {"n": 9, "at": time.time() - 8 * 86400}
        store.bump_retry("other")
        check("7 天前的旧重试记录被清理", "old" not in store._data["retries"])


# ---------------------------------------------------------------- 回归：公布后对号入座
def test_event_post_result() -> None:
    print("\n[回归] 公布后必须等到实际值再推送（窗口不能只有 4 小时）")
    import tempfile
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from src.datasource.econ_calendar import BEIJING, EconEvent
    from src.modules.levels import Levels

    levels_dict = Levels("TQQQ-USDT-SWAP", 80.0, supports=[78.0], resistances=[82.0]).as_dict()

    def mk(pub, actual):
        return EconEvent(date=pub.strftime("%Y-%m-%d"), time=pub.strftime("%H:%M"),
                         region="美国", name="美国9月CPI年率未季调(%)",
                         actual=actual, forecast=3.7, previous=3.4, importance=3)

    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))
        m.cfg._data["event"]["post_max_minutes"] = 1440
        m._symbol_levels = lambda symbol: (levels_dict, 80.0, "2026-10-14 20:30:00", 1760000000000)
        sent: list[tuple[str, str]] = []
        m.notifier.send = lambda t, b: (sent.append((t, b)),
                                        SimpleNamespace(ok=True, detail="ok"))[1]

        now = datetime.now(BEIJING)

        # 数据源的实际值常滞后几十分钟到几小时；原来硬编码 240 分钟上限会永远漏掉
        far = mk(now - timedelta(minutes=360), 3.9)
        m.calendar.fetch_range = lambda *a, **k: [far]
        m.run_events()
        rec = m.state.get_event(f"event|{far.fingerprint}") or {}
        check("公布 6 小时后取到实际值 -> 仍然对号入座推送",
              len(sent) == 1 and rec.get("post_sent") is True, f"sent={sent} rec={rec}")
        check("推送正文里是数据源的真实实际值", any("3.9" in body for _, body in sent),
              str([b[:60] for _, b in sent]))
        m.run_events()
        check("同一条不再重复推送", len(sent) == 1, str(len(sent)))

        # 数据源还没填实际值时不许编数，也不许推
        pending = mk(now - timedelta(minutes=10), None)
        m.calendar.fetch_range = lambda *a, **k: [pending]
        m.run_events()
        check("已过公布时间但数据源没给实际值 -> 不推送", len(sent) == 1, str(len(sent)))

        # 有上界：太旧的（隔夜）不再补推，避免把旧数据当新闻
        old = mk(now - timedelta(minutes=30 * 60), 3.9)
        m.calendar.fetch_range = lambda *a, **k: [old]
        m.run_events()
        check("超过 post_max_minutes 的旧事件不再补推", len(sent) == 1, str(len(sent)))

        # 回看进来的昨天事件不该把日历拖进 3 分钟一刷的「热缓存」
        check("昨天的旧事件不会触发热缓存高频刷新", m._calendar_hot([old], now) is False)


# ---------------------------------------------------------------- 回归：日历回看范围
def test_calendar_lookback() -> None:
    print("\n[回归] 日历抓取要包含昨天（隔夜才填上的实际值靠它）")
    import sys
    import types
    from datetime import datetime, timedelta
    from unittest.mock import patch

    from src.datasource.econ_calendar import BEIJING, EconCalendarClient

    asked: list[str] = []

    class FakeFrame:
        """假装 akshare 返回的 DataFrame，只用到 .empty 和 .to_dict()。"""

        def __init__(self, rows):
            self._rows = rows

        @property
        def empty(self):
            return not self._rows

        def to_dict(self, orient):
            return self._rows

    def fake_baidu(date=None):
        asked.append(date)
        return FakeFrame([{"日期": date, "时间": "20:30", "地区": "美国",
                           "事件": "美国9月CPI年率未季调(%)", "公布": "3.9",
                           "预期": "3.7", "前值": "3.4", "重要性": "3"}])

    fake_ak = types.ModuleType("akshare")
    fake_ak.news_economic_baidu = fake_baidu
    client = EconCalendarClient(days_ahead=7, lookback_days=1, day_interval_seconds=0)

    with patch.dict(sys.modules, {"akshare": fake_ak}):
        events = client.fetch_range()

    today = datetime.now(BEIJING).date()
    expect_first = (today - timedelta(days=1)).strftime("%Y%m%d")
    check("抓取从昨天开始（只从今天抓就永远拿不到隔夜的实际值）",
          asked[0] == expect_first, str(asked[:2]))
    check("覆盖天数 = days_ahead + 回看天数", len(asked) == 8, str(len(asked)))
    check("能解析出数据源的实际值", bool(events) and events[0].actual == 3.9, str(events[:1]))


# ---------------------------------------------------------------- 回归：缓存
def test_caching() -> None:
    print("\n[回归] 周K / 日历 / instrument 缓存")
    import tempfile
    import time
    from datetime import datetime, timedelta

    from src.datasource.econ_calendar import BEIJING, EconEvent

    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))

        # ---- 周K ----
        symbol = {"inst_id": "X-USDT-SWAP"}
        calls = {"n": 0, "fail": False}

        def fake_fetch(sym):
            calls["n"] += 1
            if calls["fail"]:
                raise RuntimeError("yahoo down")
            return [], "desc"

        m._fetch_weekly_candles = fake_fetch
        m._weekly_candles(symbol)
        m._weekly_candles(symbol)
        check("周K缓存期内只真实拉取一次", calls["n"] == 1, str(calls["n"]))

        ts, c, d = m._weekly_cache["X-USDT-SWAP"]
        m._weekly_cache["X-USDT-SWAP"] = (ts - m.weekly_cache_seconds - 1, c, d)
        m._weekly_candles(symbol)
        check("缓存过期后重新拉取", calls["n"] == 2, str(calls["n"]))

        calls["fail"] = True
        ts, c, d = m._weekly_cache["X-USDT-SWAP"]
        m._weekly_cache["X-USDT-SWAP"] = (ts - m.weekly_cache_seconds - 1, c, d)
        try:
            m._weekly_candles(symbol)
            check("刷新失败时回退到不太旧的缓存", True)
        except Exception as exc:
            check("刷新失败时回退到不太旧的缓存", False, str(exc))
        m._weekly_cache["X-USDT-SWAP"] = (time.time() - 7 * 3600, c, d)
        try:
            m._weekly_candles(symbol)
            check("缓存超过 6 小时且刷新失败 -> 如实报错", False, "没有抛异常")
        except RuntimeError:
            check("缓存超过 6 小时且刷新失败 -> 如实报错", True)

        # ---- 日历 ----
        now = datetime.now(BEIJING)
        far = now + timedelta(hours=30)
        near = now + timedelta(minutes=2)

        def mk(pub, name="美国9月CPI年率"):
            return EconEvent(date=pub.strftime("%Y-%m-%d"), time=pub.strftime("%H:%M"),
                             region="美国", name=name, forecast=3.7, previous=3.5, importance=3)

        cal = {"n": 0, "events": [mk(far)], "fail": False}

        def fake_cal(*a, **k):
            cal["n"] += 1
            if cal["fail"]:
                raise RuntimeError("baidu 403")
            return cal["events"]

        m.calendar.fetch_range = fake_cal
        m._calendar_events(now)
        m._calendar_events(now)
        check("日历缓存期内只抓取一次（原来每轮抓 7 天）", cal["n"] == 1, str(cal["n"]))

        m._calendar_cache = (time.time() - 4 * 60, m._calendar_cache[1])
        m._calendar_events(now)
        check("无临近事件时 4 分钟的缓存仍有效", cal["n"] == 1, str(cal["n"]))

        cal["events"] = [mk(near)]
        m._calendar_cache = (time.time() - 31 * 60, [mk(far)])
        m._calendar_events(now)
        check("缓存超过 30 分钟 -> 刷新", cal["n"] == 2, str(cal["n"]))
        m._calendar_cache = (time.time() - 4 * 60, [mk(near)])
        m._calendar_events(now)
        check("有事件 2 分钟后公布 -> 缩短缓存，4 分钟的缓存要刷新", cal["n"] == 3, str(cal["n"]))

        m._calendar_events(now, fresh=True)
        check("fresh=True（手动 --force）绕过缓存", cal["n"] == 4, str(cal["n"]))

        cal["fail"] = True
        m._calendar_cache = (time.time() - 31 * 60, [mk(far)])
        got = m._calendar_events(now)
        check("日历刷新失败时沿用旧缓存而不是丢掉整轮", len(got) == 1 and got[0].name.startswith("美国"))

        # ---- instrument ----
        api = {"n": 0, "rows": [{"instId": "X"}]}

        def fake_api(path, params):
            api["n"] += 1
            return api["rows"]

        m.okx._api = fake_api
        m.okx.get_instrument("X-USDT-SWAP", "SWAP")
        m.okx.get_instrument("X-USDT-SWAP", "SWAP")
        check("存在的合约缓存，只请求一次", api["n"] == 1, str(api["n"]))
        api["rows"] = []
        api["n"] = 0
        m.okx.get_instrument("NOPE-USDT-SWAP", "SWAP")
        m.okx.get_instrument("NOPE-USDT-SWAP", "SWAP")
        check("不存在的合约不缓存，每次都如实报告", api["n"] == 2, str(api["n"]))

    from src.http_util import HttpClient
    delay = HttpClient(backoff=2.0)._delay(1)
    check("重试日志用的退避时长真实可用（1.4~2.6s）", 1.3 < delay < 2.7, str(delay))


def _mk_candle(bj: datetime, o: float, h: float, l: float, c: float, v: float) -> Candle:
    return Candle(ts_ms=int(bj.timestamp() * 1000), open=o, high=h, low=l, close=c,
                  volume=v, confirmed=True)


# ---------------------------------------------------------------- 做空观察位
def test_intraday_levels() -> None:
    print("\n[做空] 日内关键位（前日高 / 当日高 / VWAP）")
    day1 = datetime(2026, 10, 13, 10, 0, tzinfo=BJ)
    day2 = datetime(2026, 10, 14, 10, 0, tzinfo=BJ)
    candles = [
        _mk_candle(day1, 100, 110, 95, 105, 10),
        _mk_candle(day1 + timedelta(hours=4), 105, 112, 100, 108, 30),
        _mk_candle(day2, 108, 115, 105, 110, 20),
        _mk_candle(day2 + timedelta(hours=4), 110, 118, 109, 117, 20),
    ]
    lv = derive_intraday_levels("X-USDT-SWAP", candles, 117.0)
    check("前日高点取自前一日", lv.prev_day_high == 112, str(lv.prev_day_high))
    check("当日高点取自当日", lv.day_high == 118, str(lv.day_high))
    check("当日低点取自当日", lv.day_low == 105, str(lv.day_low))
    expected_vwap = ((115 + 105 + 110) / 3 * 20 + (118 + 109 + 117) / 3 * 20) / 40
    check("VWAP 为典型价按量加权", lv.vwap is not None and abs(lv.vwap - expected_vwap) < 0.01,
          str(lv.vwap))
    check("现价下方最近压力=当日高", lv.nearest_resistance() == 118, str(lv.nearest_resistance()))
    lv2 = derive_intraday_levels("X-USDT-SWAP", candles, 119.0)
    check("现价高于所有候选时取最高候选", lv2.nearest_resistance() == 118,
          str(lv2.nearest_resistance()))
    empty = derive_intraday_levels("X-USDT-SWAP", [], 100.0)
    check("空K线不臆测（返回空位）",
          empty.day_high is None and empty.nearest_resistance() is None)


def test_orderflow_metrics() -> None:
    print("\n[做空] 盘口卖压指标（只读盘口 + 逐笔）")
    now_ms = int(time.time() * 1000)
    book = OrderBook(
        inst_id="X",
        bids=[BookLevel(100.0, 1.0), BookLevel(99.0, 1.0)],
        asks=[BookLevel(101.0, 1.0), BookLevel(105.0, 10.0), BookLevel(106.0, 1.0)],
        ts_ms=now_ms,
    )
    trades = [Trade(0, 101.0, 1.0, "sell"), Trade(0, 101.0, 1.0, "buy"),
              Trade(0, 101.0, 2.0, "sell")]
    m = compute_orderflow_metrics(book, trades, 100.0, 105.0, now_ms=now_ms)
    check("买卖量失衡为负（卖压）",
          m.imbalance is not None and abs(m.imbalance + 0.7143) < 0.001, str(m.imbalance))
    check("主动卖出占比 3/4=0.75",
          m.taker_sell_ratio is not None and abs(m.taker_sell_ratio - 0.75) < 1e-9,
          str(m.taker_sell_ratio))
    check("识别压力位附近卖墙（10 倍）",
          m.sell_wall_price == 105.0 and m.sell_wall_ratio == 10.0,
          f"{m.sell_wall_price}/{m.sell_wall_ratio}")
    check("盘口数据可用", m.usable())
    empty = compute_orderflow_metrics(None, [], 100.0, None)
    check("无盘口/逐笔时 usable=False 且不硬算",
          not empty.usable() and empty.imbalance is None and empty.taker_sell_ratio is None)


def test_short_scoring() -> None:
    print("\n[做空] 打分纯函数 score_short_signal")
    strong = OrderflowMetrics(
        imbalance=-0.4, taker_sell_ratio=0.7, sell_wall_price=105.0, sell_wall_ratio=3.0,
        trade_count=50, depth_levels=100, book_age_ms=500,
    )
    d = score_short_signal(100.0, 100.5, 100.8, strong, "range", pressure_persist=True)
    check("触及+回落+卖压 -> 告警", d.alert and d.score >= 4, str(d.as_dict()))
    check("观察位=日内压力位", d.observation_level == 100.5, str(d.observation_level))
    check("失效位=压力位上方 1%", abs(d.invalidation_level - 100.5 * 1.01) < 1e-9,
          str(d.invalidation_level))
    check("观察区距现价%（+0.50%）", d.distance_pct is not None and abs(d.distance_pct - 0.5) < 1e-6,
          str(d.distance_pct))
    check("失效位距现价%（+1.505%）",
          d.invalidation_distance_pct is not None and abs(d.invalidation_distance_pct - 1.505) < 1e-6,
          str(d.invalidation_distance_pct))

    bull = score_short_signal(100.0, 100.5, 100.8, strong, "bull", pressure_persist=True)
    check("周线多头：逆势扣分 2", bull.score == d.score - 2, f"{bull.score} vs {d.score}")

    far = score_short_signal(90.0, 100.5, 100.6, strong, "range")
    check("远离压力位 -> 不告警", not far.alert, str(far.as_dict()))

    neutral = OrderflowMetrics(
        imbalance=0.1, taker_sell_ratio=0.4, sell_wall_price=None, sell_wall_ratio=None,
        trade_count=10, depth_levels=20, book_age_ms=200,
    )
    mid = score_short_signal(100.0, 100.5, 100.1, neutral, "range")
    check("无卖压、无拒绝 -> 分低不告警", not mid.alert, str(mid.as_dict()))

    no_res = score_short_signal(100.0, None, None, strong, "range")
    check("缺压力位 -> data_ok=False 不告警", not no_res.alert and not no_res.data_ok)
    no_of = score_short_signal(100.0, 100.5, 100.8, None, "range")
    check("缺盘口 -> data_ok=False 不告警", not no_of.alert and not no_of.data_ok)


def test_short_alert_wording() -> None:
    print("\n[做空] 推送文案：只说「失效位」，不碰红线")
    import tempfile

    now_ms = int(time.time() * 1000)
    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))
        symbol = {"inst_id": "TQQQ-USDT-SWAP", "name": "TQQQ", "inst_type": "SWAP"}
        ticker = Ticker("TQQQ-USDT-SWAP", 100.0, 98.0, 101.0, 97.0, 1000.0, now_ms)
        day1 = datetime(2026, 10, 13, 10, 0, tzinfo=BJ)
        day2 = datetime(2026, 10, 14, 10, 0, tzinfo=BJ)
        candles = [
            _mk_candle(day1, 100, 110, 95, 105, 10),
            _mk_candle(day2, 108, 100.8, 99.0, 100.0, 20),
        ]
        intraday = derive_intraday_levels("TQQQ-USDT-SWAP", candles, 100.0)
        book = OrderBook(
            "TQQQ-USDT-SWAP",
            bids=[BookLevel(99.9, 1.0)],
            asks=[BookLevel(100.5, 8.0), BookLevel(100.6, 1.0)],
            ts_ms=now_ms,
        )
        trades = [Trade(0, 100.0, 1.0, "sell"), Trade(0, 100.0, 1.0, "buy")]
        metrics = compute_orderflow_metrics(book, trades, 100.0, 100.5, now_ms=now_ms)
        decision = score_short_signal(100.0, 100.5, 100.6, metrics, "bull")
        body = m._build_short_alert(symbol, ticker, intraday, metrics, decision,
                                    "美盘(21:30-04:00)", "bull")
        text = body.render()
        check("标题含「做空观察位」", "做空观察位" in body.title(), body.title())
        check("正文含「失效位」", "失效位" in text)
        check("正文含距现价百分比", "距现价" in text)
        check("信号类型正确", SIGNAL_SHORT in text)
        check("不含「止损」字样", "止损" not in text)
        check("不含「开仓」字样", "开仓" not in text)
        check("红线过滤器无违规", m.safety.violations(text) == [], str(m.safety.violations(text)))

        # 同品种近期「国际大事」给过偏多时，做空观察位正文必须显式提示两轨口径不同
        m._note_direction("TQQQ-USDT-SWAP", "偏多", SIGNAL_WORLD)
        body2 = m._build_short_alert(symbol, ticker, intraday, metrics, decision,
                                     "美盘(21:30-04:00)", "bull")
        text2 = body2.render()
        check("跨轨相反方向 -> 正文带口径提示", "跨轨口径提示" in text2, text2[-260:])
        check("提示里点出国际大事的偏多", "国际大事" in text2 and "偏多" in text2)
        check("带提示后仍无红线违规", m.safety.violations(text2) == [])
        check("卖压措辞由真实数据支撑（本例买卖失衡 -0.80）",
              "盘口卖压信号" in text and "盘口买卖失衡 -0.80" in text, text[:160])

        # 完全没有卖压证据时，标题不许写死「盘口呈现卖压」
        no_sell = compute_orderflow_metrics(
            OrderBook("TQQQ-USDT-SWAP", bids=[BookLevel(99.9, 1.0)],
                      asks=[BookLevel(100.5, 1.0)], ts_ms=now_ms),
            [Trade(0, 100.0, 1.0, "buy"), Trade(0, 100.0, 1.0, "sell")],
            100.0, 100.5, now_ms=now_ms,
        )
        body3 = m._build_short_alert(symbol, ticker, intraday, no_sell, decision,
                                     "美盘(21:30-04:00)", "bull")
        check("无卖压证据 -> 标题直说无卖压确认，不硬写「盘口呈现卖压」",
              "盘口暂无明显卖压确认" in body3.render(), body3.render()[:160])


def test_cross_track_conflict_note() -> None:
    print("\n[跨轨] 方向备忘与冲突提示的判定规则")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))
        inst = "CL-USDT-SWAP"
        check("没有备忘 -> 不提示", m._cross_track_note(inst, SIGNAL_SHORT, "偏空") is None)

        m._note_direction(inst, "偏多", SIGNAL_WORLD)
        note = m._cross_track_note(inst, SIGNAL_SHORT, "偏空")
        check("国际大事偏多 vs 做空观察位 -> 提示", bool(note), str(note))
        check("提示同时点明两条轨与两个方向",
              bool(note) and "国际大事" in note and "偏多" in note
              and "做空观察位" in note and "偏空" in note, str(note))
        check("另一条轨同方向 -> 不提示",
              m._cross_track_note(inst, SIGNAL_SHORT, "偏多") is None)
        check("同一条轨 -> 不提示",
              m._cross_track_note(inst, SIGNAL_WORLD, "偏空") is None)

        m._note_direction(inst, "偏空", SIGNAL_SHORT)
        check("反向对称：做空观察位 vs 国际大事偏多 -> 提示",
              bool(m._cross_track_note(inst, SIGNAL_WORLD, "偏多")))

        m._note_direction(inst, "偏多", SIGNAL_WORLD, ts=time.time() - 7 * 3600)
        check("超出时间窗（7 小时前）-> 不再提示",
              m._cross_track_note(inst, SIGNAL_SHORT, "偏空") is None)


def test_fake_break_followup() -> None:
    print("\n[做空·方案A] 上破失效位后跌回 -> 补发「假突破回落」观察（与压力位观察一眼可辨）")
    import tempfile
    from types import SimpleNamespace

    now_ms = int(time.time() * 1000)
    inst = "TQQQ-USDT-SWAP"
    key = f"shortwatch|{inst}"
    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))
        m.notifier.send = lambda t, b: SimpleNamespace(ok=True, detail="")
        symbol = {"inst_id": inst, "name": "TQQQ", "inst_type": "SWAP"}
        ticker = Ticker(inst, 100.2, 105.0, 106.0, 97.0, 1000.0, now_ms)
        day1 = datetime(2026, 10, 13, 10, 0, tzinfo=BJ)
        day2 = datetime(2026, 10, 14, 10, 0, tzinfo=BJ)
        candles = [
            _mk_candle(day1, 100, 110, 95, 105, 10),
            _mk_candle(day2, 108, 100.8, 99.0, 100.0, 20),
        ]
        intraday = derive_intraday_levels(inst, candles, 100.2)
        strong = OrderflowMetrics(imbalance=-0.5, taker_sell_ratio=0.8, sell_wall_price=100.5,
                                  sell_wall_ratio=2.5, trade_count=100, depth_levels=20,
                                  book_age_ms=200)
        neutral = OrderflowMetrics(imbalance=0.1, taker_sell_ratio=0.4, sell_wall_price=None,
                                   sell_wall_ratio=None, trade_count=10, depth_levels=20,
                                   book_age_ms=200)
        decision = score_short_signal(100.2, 100.5, 100.6, strong, "range")
        stamp = datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")

        def step(last_close, metrics, suppress=False):
            return m._fake_break_followup(symbol, ticker, intraday, metrics, last_close,
                                          "美盘(21:30-04:00)", "range", decision,
                                          suppress=suppress, force=False)

        check("没有锚点 -> 什么都不做", step(100.0, strong) == [])

        m._open_short_anchor(inst, decision, stamp)
        anchor = m.state.get_session(key) or {}
        check("推送后登记锚点：watching 且压力位/失效位已冻结",
              anchor.get("state") == "watching"
              and anchor.get("resistance") == decision.observation_level
              and anchor.get("invalidation") == decision.invalidation_level, str(anchor))

        check("本轮已有贴近压力位的推送 -> 不重复补发",
              step(100.2, strong, suppress=True) == [])
        check("suppress 不改变锚点状态",
              (m.state.get_session(key) or {}).get("state") == "watching")

        out = step(decision.invalidation_level + 1.0, strong)
        anchor = m.state.get_session(key) or {}
        check("收盘站上失效位 -> 转「已上破」且本条不推送",
              out == [] and anchor.get("state") == "broken", str(anchor))
        check("破位高点被记录", float(anchor.get("peak") or 0) >= decision.invalidation_level + 1.0,
              str(anchor.get("peak")))

        out = step(100.2, strong)
        text = " ".join(out)
        check("上破后跌回且卖压确认 -> 补发一条", len(out) == 1, str(len(out)))
        check("标题写「假突破回落」", "假突破回落" in text, text[:60])
        check("首行事件句是「冲高回落」", "⬇️ 冲高回落" in text, text[:200])
        check("正文标题改成「假突破回落观察位」", "假突破回落观察位" in text)
        check("带破位高点与冻结锚点说明", "破位高点" in text and "锚点压力位" in text)
        check("交代与上一条压力位观察的关系", "与上一条的关系" in text)
        check("红线过滤器无违规", m.safety.violations(text) == [], str(m.safety.violations(text)))
        check("补发后标记 pushed，不重复推", (m.state.get_session(key) or {}).get("pushed") is True)
        check("同一锚点只补发一次", step(100.1, strong) == [])

        # 两条消息必须一眼可辨（此前反馈：两类观察位长得太像）
        normal = m._build_short_alert(symbol, ticker, intraday, strong, decision,
                                      "美盘(21:30-04:00)", "range")
        n_text = normal.render()
        check("正常变体首行是「贴近压力位」", "⚡ 贴近压力位" in n_text, n_text[:200])
        check("正常变体标题带类型与价位",
              "做空观察位" in normal.title() and "贴近压力位" in normal.title(), normal.title())
        check("正常变体不出现「冲高回落」字样", "冲高回落" not in n_text)
        check("两条消息的正文标题不同",
              "## 【TQQQ】假突破回落观察位" in text and "## 【TQQQ】做空观察位" in n_text,
              f"{text.splitlines()[0]} vs {n_text.splitlines()[0]}")
        check("两条消息的标题也不同",
              "假突破回落" in text.split("\n")[0] or "假突破回落" in text,
              normal.title())

        m._open_short_anchor(inst, decision, stamp)
        step(decision.invalidation_level + 1.0, strong)
        check("跌回但盘口没有卖压确认 -> 不补发", step(100.2, neutral) == [])

        m.state.set_session(key, {"resistance": 100.5, "invalidation": 101.505, "peak": 101.505,
                                  "state": "broken", "opened_ts": time.time() - 13 * 3600,
                                  "pushed": False, "at": stamp})
        check("锚点超过 12 小时 -> 清除且不推送", step(100.2, strong) == [])
        check("陈旧锚点已被清掉", not (m.state.get_session(key) or {}))


def test_dry_run_writes_nothing() -> None:
    print("\n[演练] dry-run 不写真实状态（冷却 / 面板历史）")
    import tempfile
    from types import SimpleNamespace

    from run import PUSH_SENT
    from src.alert import Alert

    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))
        history_file = Path(td) / "alerts.jsonl"
        m.dry_run = True
        m.notifier.send = lambda t, b: SimpleNamespace(ok=True, detail="")
        alert = Alert(
            symbol="X（X-USDT-SWAP）", what_happened="演练",
            time_text="2026-10-15 22:00:00 北京时间", price=100.0,
            signal_type=SIGNAL_SHORT, short_name="X",
            inst_id="X-USDT-SWAP", ts_ms=int(time.time() * 1000),
        )
        fp = "short|X-USDT-SWAP|100"
        status = m.push_status("标题", "正文", fp, force=True, alert=alert)
        check("演练仍按「已发出」返回", status == PUSH_SENT, status)
        check("演练不写冷却状态（否则会压制真实告警）",
              m.state.is_in_cooldown(fp, 240) == (False, 0.0),
              str(m.state.is_in_cooldown(fp, 240)))
        check("演练不写面板历史",
              (not history_file.exists()) or history_file.stat().st_size == 0,
              str(history_file))


def test_multi_sendkey() -> None:
    print("\n[推送] 多个 SendKey（一次推给多个用户）")
    from types import SimpleNamespace

    from src.http_util import HttpError
    from src.notify.serverchan import ServerChanNotifier, mask_key, parse_send_keys

    keys = parse_send_keys("SCTaaaa11112222bbbb, SCTcccc33334444dddd;SCTeeee55556666ffff")
    check("逗号/分号分隔的多个 key 都解析出来", len(keys) == 3, str(keys))
    check("旧的单 key 写法仍兼容",
          parse_send_keys("SCTonly111122223333") == ["SCTonly111122223333"])
    check("重复 key 只保留一个", len(parse_send_keys("SCTsame,SCTsame")) == 1)
    check("空值/模板占位符被剔除",
          parse_send_keys("  , SCT在这里填你的sendkey , sk-在这里填你的-deepseek-key ") == [])
    check("模板占位符不算「已配置」",
          not ServerChanNotifier("SCT在这里填你的sendkey", dry_run=True).configured)
    check("日志脱敏：只留头尾",
          mask_key("SCTaaaaaaaaaaaa1111bbbb2222cccc") == "SCTaaaaa…cccc")

    class FakeHttp:
        """按 key 决定成功/失败，用于验证「部分失败」的语义。"""

        def __init__(self, fail_keys: tuple[str, ...] = ()) -> None:
            self.fail_keys = set(fail_keys)
            self.hit: list[str] = []

        def request(self, method: str, url: str, data=None):
            key = url.rsplit("/", 1)[-1].removesuffix(".send")
            self.hit.append(key)
            if key in self.fail_keys:
                raise HttpError("boom")
            return SimpleNamespace(status_code=200,
                                   json=lambda: {"code": 0, "data": {"pushid": "x"}})

    two = "SCTkeyA000000000000,SCTkeyB000000000000"
    n = ServerChanNotifier(two, max_retries=0, retry_backoff_seconds=0.0)
    n.http = FakeHttp()
    r = n.send("标题", "正文")
    check("两个目标都成功 -> ok=True", r.ok and len(n.http.hit) == 2, str(r.detail))
    check("每个目标各发一次",
          n.http.hit == ["SCTkeyA000000000000", "SCTkeyB000000000000"], str(n.http.hit))

    n2 = ServerChanNotifier(two, max_retries=0, retry_backoff_seconds=0.0)
    n2.http = FakeHttp(fail_keys=("SCTkeyB000000000000",))
    r2 = n2.send("标题", "正文")
    check("有一个目标失败 -> 整体判失败（上层会重发，不漏收）", not r2.ok, str(r2.detail))
    check("失败详情标明是哪个目标挂了", "失败" in r2.detail, str(r2.detail))

    r3 = ServerChanNotifier("").send("标题", "正文")
    check("一个 key 都没有 -> 如实报 missing_send_key",
          not r3.ok and r3.detail == "missing_send_key", str(r3.detail))


def test_calendar_columns() -> None:
    print("\n[日历] akshare 真实列名映射（防真实值「公布」被漏掉）")
    from src.datasource.econ_calendar import EconCalendarClient

    row = {
        "日期": "2026-10-14", "时间": "20:30", "地区": "美国",
        "事件": "美国9月CPI年率未季调(%)",
        "公布": "3.70", "预期": "3.60", "前值": "3.40", "重要性": "2",
    }
    e = EconCalendarClient._build_event(row)
    check("真实列名「公布」能解析成实际值", e is not None and e.actual == 3.70,
          str(e and e.actual))
    check("预期解析成 3.60", e.forecast == 3.60, str(e.forecast))
    check("前值解析成 3.40", e.previous == 3.40, str(e.previous))
    check("重要性解析成 2", e.importance == 2, str(e.importance))
    check("原始字段里保留了实际值便于回溯", e.raw.get("actual") == "3.70", str(e.raw))

    legacy = {k: v for k, v in row.items() if k != "公布"}
    legacy["今值"] = "3.70"
    e2 = EconCalendarClient._build_event(legacy)
    check("旧列名「今值」仍兼容", e2 is not None and e2.actual == 3.70, str(e2 and e2.actual))


# ---------------------------------------------------------------- 面板：告警历史
def test_alert_history() -> None:
    print("\n[面板] 告警历史落盘 / 读回 / 裁剪")
    import tempfile

    from src.dashboard.history import AlertHistory, AlertRecord

    with tempfile.TemporaryDirectory() as tmp:
        # 目录不存在也要能自动建（state/ 可能被手动清空过）
        history = AlertHistory(Path(tmp) / "sub" / "alerts.jsonl")
        record = AlertRecord(
            ts_ms=1792051721469, recorded_at="2026-10-15 16:08:41",
            inst_id="TQQQ-USDT-SWAP", symbol="TQQQ", short_name="TQQQ",
            signal_type="做空观察位", title="【TQQQ】做空观察位｜现价 74.23",
            what_happened="价格触及做空观察位", price=74.23, support=69.76, resistance=72.78,
            fingerprint="short|TQQQ-USDT-SWAP|1", pushed_at="2026-10-15 03:23:34",
            source="log_backfill",
        )
        check("目录不存在时也能写入", history.append(record), str(history.path))

        # 坏行 / 空行 / BOM 都不该让读取炸掉（用户手改文件是常态）
        with open(history.path, "a", encoding="utf-8") as fh:
            fh.write('\ufeff{"ts_ms": 1, "inst_id": "X", "signal_type": "趋势"}\n')
            fh.write("{这不是合法json\n\n")
        loaded = history.load()
        check("坏行被跳过、好行照读", len(loaded) == 2, f"读到 {len(loaded)} 条")
        check("读回按时间升序",
              [r.ts_ms for r in loaded] == [1, 1792051721469], str([r.ts_ms for r in loaded]))

        line = record.to_line()
        check("落盘中文不转义（方便直接 tail 文件）", "做空观察位" in line, line[:50])
        check("序列化往返字段一致", AlertRecord.from_line(line) == record)
        check("缺失值落成 null 而不是 0",
              '"price": null' in AlertRecord().to_line(), AlertRecord().to_line())

        check("按品种过滤生效", len(history.load(inst_id="TQQQ-USDT-SWAP")) == 1)
        check("按类型过滤生效", len(history.load(signal_type="趋势")) == 1)
        check("按起始时间过滤生效", len(history.load(since_ms=1792051721469)) == 1)

        history.append(AlertRecord(ts_ms=None, inst_id="X", signal_type="趋势", title="无时间戳"))
        removed = history.prune(keep_days=-1, max_lines=0)
        check("过期记录会被裁掉", removed == 2, f"删除 {removed} 条")
        rest = history.load()
        check("没有时间戳的记录不会被误删", len(rest) == 1 and rest[0].ts_ms is None, str(rest))

        history.append(record)
        history.append(record)
        check("max_lines 生效", history.prune(keep_days=36500, max_lines=1) == 2,
              f"当前 {len(history.load())} 条")


def test_alert_record_from_alert() -> None:
    print("\n[面板] Alert / EventAlert → 落盘记录（字段映射）")
    from src.alert import SIGNAL_EVENT, SIGNAL_SHORT, Alert, EventAlert
    from src.dashboard.history import AlertRecord, record_from_alert

    alert = Alert(
        symbol="TQQQ（TQQQ-USDT-SWAP）", what_happened="价格触及做空观察位",
        time_text="2026-10-15 03:23:34 北京时间", price=74.23, support=69.76, resistance=72.78,
        signal_type=SIGNAL_SHORT, short_name="TQQQ",
        inst_id="TQQQ-USDT-SWAP", ts_ms=1792051721469,
    )
    record = record_from_alert(alert, "【TQQQ】做空观察位｜现价 74.23", "short|TQQQ-USDT-SWAP|1",
                               body="# 做空观察位\n\n压力位 72.78 附近有卖墙")
    check("品种标识进了记录", record.inst_id == "TQQQ-USDT-SWAP", record.inst_id)
    check("数据时间进了记录（K线对齐的唯一依据）", record.ts_ms == 1792051721469, str(record.ts_ms))
    check("类型/价格/关键位都对上",
          (record.signal_type, record.price, record.support, record.resistance)
          == (SIGNAL_SHORT, 74.23, 69.76, 72.78))
    check("落盘时间自动补上", len(record.recorded_at) == 19, record.recorded_at)
    check("指纹被保留（便于回溯是哪条信号）", record.fingerprint == "short|TQQQ-USDT-SWAP|1")
    check("推送正文也落盘（面板点开消息能看到原文）",
          record.body.startswith("# 做空观察位") and "body" in record.to_line(), record.body[:20])
    check("正文往返不丢", AlertRecord.from_line(record.to_line()).body == record.body)

    event = EventAlert(
        headline="美国9月CPI年率未季调(%)", stage="pre", happened="今晚 20:30 公布",
        time_text="2026-10-14 20:30 北京时间", body="剧本", short_name="TQQQ", price=81.2,
        inst_id="TQQQ-USDT-SWAP", ts_ms=1792051721469,
    )
    event_record = record_from_alert(event, "【TQQQ】事件预警：CPI", "event|1")
    check("EventAlert 没有 signal_type 时归为「事件」",
          event_record.signal_type == SIGNAL_EVENT, event_record.signal_type)
    check("EventAlert 的 happened 也收进记录",
          event_record.what_happened == "今晚 20:30 公布", event_record.what_happened)
    check("现价缺失时落成 None（不是 0）",
          record_from_alert(Alert(symbol="X", what_happened="x", time_text="t", price=None),
                            "t", "f").price is None)


def test_bar_alignment() -> None:
    print("\n[面板] 告警与 K 线的时间对齐（口径用真实 OKX 样本核对）")
    from src.dashboard.history import alert_index_for_ts, bar_ms, bar_start_ms

    check("bar_ms 换算正确",
          (bar_ms("5m"), bar_ms("15m"), bar_ms("1H"), bar_ms("4H"), bar_ms("1D"), bar_ms("1W"))
          == (300000, 900000, 3600000, 14400000, 86400000, 604800000))
    check("认不出的周期不硬猜", bar_ms("7x") is None and bar_ms("") is None)

    check("5m 按 UTC 5 分钟对齐", bar_start_ms(1792051721469, "5m") == 1792051500000,
          str(bar_start_ms(1792051721469, "5m")))
    check("1H 按 UTC 整点对齐", bar_start_ms(1792051721469, "1H") == 1792051200000,
          str(bar_start_ms(1792051721469, "1H")))

    # 实测：OKX 日K 的开盘是北京时间 0 点（ts=1791993600000 → 北京时间 2026-10-15 00:00）
    day = 1791993600000
    check("1D 按北京时间零点对齐（真实样本）",
          bar_start_ms(day + 5 * 3600 * 1000, "1D") == day,
          str(bar_start_ms(day + 5 * 3600 * 1000, "1D")))
    check("1D 若按 UTC 取模会错一根（回归防线）",
          bar_start_ms(day + 3600 * 1000, "1D") != (day + 3600 * 1000) // 86400000 * 86400000)

    # 实测：OKX 周K 开盘是北京时间周一 0 点（ts=1791734400000 → 北京时间 2026-10-12 周一）
    week = 1791734400000
    check("1W 按北京时间周一零点对齐（真实样本）",
          bar_start_ms(week + 3 * 86400000, "1W") == week,
          str(bar_start_ms(week + 3 * 86400000, "1W")))
    check("周内任意一天都落到同一根周K",
          bar_start_ms(week, "1W") == bar_start_ms(week + 6 * 86400000, "1W") == week)

    times = [1000, 2000, 3000]
    check("告警落在哪根K线：取最后一根起始时间 <= 告警时间", alert_index_for_ts(times, 2500) == 1)
    check("正好压在边界上归到该根", alert_index_for_ts(times, 2000) == 1)
    check("早于全部K线返回 -1", alert_index_for_ts(times, 500) == -1)
    check("晚于全部K线归到最后一根", alert_index_for_ts(times, 99999) == 2)
    check("空列表 / 无时间不炸",
          alert_index_for_ts([], 100) == -1 and alert_index_for_ts(times, None) == -1)


def test_dashboard_readonly() -> None:
    print("\n[面板] 只读红线（控制页只改运行期偏好，不得接交易/推送/大模型）")
    root = Path(__file__).resolve().parent.parent
    targets = [
        root / "src" / "dashboard" / "__init__.py",
        root / "src" / "dashboard" / "history.py",
        root / "src" / "dashboard" / "service.py",
        root / "src" / "dashboard" / "server.py",
        root / "tools" / "dashboard.py",
        root / "tools" / "backfill_alerts.py",
        root / "frontend" / "app.js",
        root / "frontend" / "index.html",
    ]
    # 任何面板文件都不许出现：交易接口 / 推送发送 / 大模型调用 / 策略实现 / 除 POST 以外的写方法
    forbidden = ["do_PUT", "do_DELETE", "/api/v5/trade", "place-order",
                 "ServerChanNotifier", "deepseek", "llm",
                 "src.strategy", "PushGate"]
    problems: list[str] = []
    for path in targets:
        text = path.read_text(encoding="utf-8", errors="replace")
        for word in forbidden:
            if word in text:
                problems.append(f"{path.name} 含 {word}")
    check("面板代码里没有交易接口 / 推送发送 / 大模型调用",
          not problems, "; ".join(problems))

    server_text = (root / "src" / "dashboard" / "server.py").read_text(encoding="utf-8")
    # 写入口只允许一个 do_POST；除了它再无别的写 handler
    check("服务端写操作只有 do_POST（没有 PUT / DELETE）",
          "def do_POST" in server_text and "def do_PUT" not in server_text
          and "def do_DELETE" not in server_text)
    # 而且 do_POST 只服务 /api/control（不许顺路开出别的写路径）
    post_body = server_text.split("def _route_post", 1)[-1]
    read_paths = ["/api/kline", "/api/snapshot", "/api/symbols", "/api/alerts", "/api/health"]
    check("do_POST 只服务 /api/control 之下的控制接口",
          "/api/control" in post_body and not any(p in post_body for p in read_paths),
          str([p for p in read_paths if p in post_body]))


def test_strategy_isolation() -> None:
    print("\n[归类] 推送策略独立在 src/strategy：不依赖前端、不含交易接口")
    root = Path(__file__).resolve().parent.parent
    strategy_root = root / "src" / "strategy"

    # 1) 策略骨架必须存在（这是「看 git 只看策略」的入口）
    must_exist = [
        strategy_root / "gate.py",
        strategy_root / "deferral.py",
        strategy_root / "fingerprint.py",
        strategy_root / "wording.py",
        strategy_root / "memo.py",
        strategy_root / "tracks" / "trend.py",
        strategy_root / "tracks" / "event.py",
        strategy_root / "tracks" / "world.py",
        strategy_root / "tracks" / "session.py",
        strategy_root / "tracks" / "short.py",
    ]
    missing = [str(p.relative_to(root)) for p in must_exist if not p.is_file()]
    check("src/strategy 策略模块齐备（闸口 / 延迟补发 / 指纹 / 措辞 / 备忘 / 五条轨道）",
          not missing, "缺少：" + ", ".join(missing))

    # 2) 轨道实现必须真的搬走了，不许再长回 run.py
    #    （push_status / _defer_push / _flush_pending 只保留 1-3 行委托壳，测试会直接调用，故不在此列）
    run_text = (root / "run.py").read_text(encoding="utf-8", errors="replace")
    must_be_gone = [
        "def run_trend", "def _build_trend_alert", "def _trend_fallback_text",
        "def _build_session_alert", "def _short_fallback_text", "def _build_short_alert",
        "def _fake_break_followup", "def _world_actions", "def _cross_track_note",
        "def _emit_pre_script", "def _emit_post_result",
    ]
    still_there = [name for name in must_be_gone if name in run_text]
    check("轨道实现已从 run.py 搬进 src/strategy/tracks（没有残留副本）",
          not still_there, "run.py 里仍有：" + ", ".join(still_there))

    # 3) 策略层不得反向依赖前端（也不得反向依赖 run.py）
    frontend_words = ["src.dashboard", "http.server", "do_POST", "frontend", "import run", "from run"]
    leaks: list[str] = []
    for path in sorted(strategy_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for word in frontend_words:
            if word in text:
                leaks.append(f"{path.name} 含 {word}")
    check("策略层不依赖前端 / 面板 / 入口模块", not leaks, "; ".join(leaks))

    # 4) 策略层不得出现真实下单能力
    trade_words = ["/api/v5/trade", "place_order", "create_order", "cancel_order",
                   "ccxt", "submit_order", "broker"]
    trades: list[str] = []
    for path in sorted(strategy_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for word in trade_words:
            if word in text:
                trades.append(f"{path.name} 含 {word}")
    check("策略层不含任何下单 / 撤单 / 券商接口", not trades, "; ".join(trades))

    # 5) 常量 re-export 兼容层必须保留（脚本与测试都从 run 里取）
    from run import (MAX_PUSH_RETRIES, PRIORITY_PREFIX, PUSH_DEFERRED,  # noqa: F401
                     PUSH_FAILED, PUSH_SENT, PUSH_SKIPPED, STATE_SHORT)
    check("run.py 仍 re-export 推送常量（旧调用方不受影响）",
          (PUSH_SENT, PUSH_DEFERRED, PUSH_FAILED, PUSH_SKIPPED, MAX_PUSH_RETRIES,
           PRIORITY_PREFIX, STATE_SHORT)
          == ("sent", "deferred", "failed", "skipped", 5, "【重点】",
              {"bull": "多头", "bear": "空头", "range": "震荡", "unknown": "数据不足"}))


def test_control_store() -> None:
    print("\n[控制端] 控制项存储：品种开关 / 免打扰 / 重点提醒 / 持仓")
    import tempfile

    from src.control import ControlError, ControlStore
    from src.datasource.econ_calendar import BEIJING

    valid = ["TQQQ-USDT-SWAP", "CL-USDT-SWAP"]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "control.json"
        store = ControlStore(path, valid_inst_ids=valid)

        check("默认全部跟随 config（无覆盖）",
              store.symbol_override("TQQQ-USDT-SWAP") is None and not store.dnd_enabled()
              and store.priority_symbols() == [] and store.priority_tracks() == []
              and store.position("TQQQ-USDT-SWAP") is None)

        # ---- 逐品种开关 ----
        store.set_symbol_enabled("CL-USDT-SWAP", False)
        check("逐品种开关：关掉生效", store.symbol_override("CL-USDT-SWAP") is False)
        store.set_symbol_enabled("CL-USDT-SWAP", None)
        check("清空覆盖 = 回到 config", store.symbol_override("CL-USDT-SWAP") is None)
        try:
            store.set_symbol_enabled("DOGE-USDT-SWAP", False)
            check("未知品种被拒绝", False, "没有报错")
        except ControlError:
            check("未知品种被拒绝", True)

        # ---- 免打扰：跨零点 + 星期限制 ----
        store.set_dnd(True, [{"name": "夜间", "start": "23:00", "end": "07:00"}])
        check("免打扰开关生效", store.dnd_enabled())
        check("跨零点：23:30 命中",
              store.is_dnd_active(datetime(2026, 10, 16, 23, 30, tzinfo=BEIJING)))
        check("跨零点：03:00 命中",
              store.is_dnd_active(datetime(2026, 10, 16, 3, 0, tzinfo=BEIJING)))
        check("跨零点：12:00 不命中",
              not store.is_dnd_active(datetime(2026, 10, 16, 12, 0, tzinfo=BEIJING)))
        store.set_dnd(False, [{"name": "夜间", "start": "23:00", "end": "07:00"}])
        check("总开关关掉后不再免打扰",
              not store.is_dnd_active(datetime(2026, 10, 16, 23, 30, tzinfo=BEIJING)))
        store.set_dnd(True, [{"name": "周一夜间", "start": "23:00", "end": "07:00",
                              "weekdays": [0]}])
        check("限定星期：周二不在窗口",
              not store.is_dnd_active(datetime(2026, 10, 20, 23, 30, tzinfo=BEIJING)))
        check("限定星期：周一在窗口",
              store.is_dnd_active(datetime(2026, 10, 19, 23, 30, tzinfo=BEIJING)))
        try:
            store.set_dnd(True, [{"name": "坏", "start": "oops", "end": "07:00"}])
            check("非法时段被拒绝", False, "没有报错")
        except ControlError:
            check("非法时段被拒绝", True)

        # ---- 重点提醒（品种 + 轨道）----
        store.set_priority(["TQQQ-USDT-SWAP"], ["world"])
        check("重点品种命中", store.is_priority("trend", "TQQQ-USDT-SWAP"))
        check("重点轨道命中", store.is_priority("world", "CL-USDT-SWAP"))
        check("非重点不误判", not store.is_priority("trend", "CL-USDT-SWAP"))
        try:
            store.set_priority([], ["不存在的轨道"])
            check("未知轨道被拒绝", False, "没有报错")
        except ControlError:
            check("未知轨道被拒绝", True)

        # ---- 持仓覆盖 ----
        store.set_position("CL-USDT-SWAP", {"side": "short", "entry_price": 61.2,
                                            "margin_usdt": 20, "leverage": 3, "note": "空单"})
        pos = store.position("CL-USDT-SWAP") or {}
        check("持仓写入并可读回",
              pos.get("side") == "short" and pos.get("entry_price") == 61.2
              and pos.get("leverage") == 3.0, str(pos))
        try:
            store.set_position("CL-USDT-SWAP", {"side": "long", "entry_price": -1})
            check("持仓价必须为正", False, "没有报错")
        except ControlError:
            check("持仓价必须为正", True)
        store.set_position("CL-USDT-SWAP", {"side": "", "entry_price": "", "margin_usdt": "",
                                            "leverage": "", "note": ""})
        check("留空 = 清除持仓覆盖", store.position("CL-USDT-SWAP") is None)

        # ---- 落盘 + 重载 ----
        store.set_priority(["CL-USDT-SWAP"], [])
        check("重载后读到最新控制项",
              ControlStore(path, valid_inst_ids=valid).priority_symbols() == ["CL-USDT-SWAP"])


def test_control_dnd_defer() -> None:
    print("\n[控制端] 免打扰延迟补发：非重点留队补发，重点照常推")
    import tempfile
    from types import SimpleNamespace

    from run import PRIORITY_PREFIX, PUSH_DEFERRED, PUSH_SENT

    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))
        # 用 monkeypatch 固定「处于免打扰」，避免测试受当前时刻影响；
        # 时间匹配逻辑本身由 test_control_store 用固定时刻覆盖。
        m.control.set_dnd(True, [{"name": "全天", "start": "00:00", "end": "23:59"}])
        m.control.is_dnd_active = lambda now=None: True  # type: ignore[assignment]

        sent: list[tuple[str, str]] = []

        def fake_send(title: str, body: str):
            sent.append((title, body))
            return SimpleNamespace(ok=True, detail="")

        m.notifier.send = fake_send

        fp = "trend|TQQQ-USDT-SWAP|bear"
        status = m.push_status("趋势转空", "正文", fp)
        check("免打扰期间非重点不发送", status == PUSH_DEFERRED and sent == [],
              f"{status} {sent}")
        check("进入延迟队列", len(m.pending.items()) == 1, str(m.pending.items()))
        check("已记冷却（下轮不重复入队）", m.state.is_in_cooldown(fp, 240)[0])

        m.control.set_priority(["TQQQ-USDT-SWAP"], [])
        fp2 = "trend|TQQQ-USDT-SWAP|bull"
        status2 = m.push_status("趋势转多", "正文", fp2)
        check("重点品种免打扰期间照常推送", status2 == PUSH_SENT and len(sent) == 1,
              f"{status2} {sent}")
        check("重点标题带醒目前缀",
              bool(sent) and sent[0][0].startswith(PRIORITY_PREFIX), sent[0][0] if sent else "")
        check("重点消息不进延迟队列", len(m.pending.items()) == 1)

        m.control.set_priority([], ["session"])
        status3 = m.push_status("盘中波动", "正文", "session|CL-USDT-SWAP|30m")
        check("重点轨道免打扰期间照常推送", status3 == PUSH_SENT and len(sent) == 2,
              f"{status3} {sent}")

        m.control.is_dnd_active = lambda now=None: False  # type: ignore[assignment]
        flushed = m._flush_pending()
        check("退出免打扰后补发 1 条", flushed == 1, str(flushed))
        check("队列已清空", m.pending.items() == [], str(m.pending.items()))
        check("补发的正是被延迟的那条", any(t == "趋势转空" for t, _ in sent), str(sent))


def test_control_position_override() -> None:
    print("\n[控制端] 持仓覆盖：控制台填的优先于 config")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))
        symbol = m.cfg.symbols[0]
        inst_id = symbol["inst_id"]
        base_entry = (symbol.get("position") or {}).get("entry_price")
        check("默认用 config 里的真实持仓",
              m._effective_position(symbol).get("entry_price") == base_entry,
              str(m._effective_position(symbol)))
        m.control.set_position(inst_id, {"side": "long", "entry_price": 90.0,
                                        "margin_usdt": 30, "leverage": 5, "note": "改过"})
        over = m._effective_position(symbol)
        check("控制台填的持仓生效",
              over.get("entry_price") == 90.0 and over.get("leverage") == 5.0, str(over))
        m.control.set_position(inst_id, {"side": "", "entry_price": "", "margin_usdt": "",
                                        "leverage": "", "note": ""})
        check("清除后回到 config 持仓",
              m._effective_position(symbol).get("entry_price") == base_entry,
              str(m._effective_position(symbol)))


def test_control_service() -> None:
    print("\n[控制端] 控制页接口：快照 + 写入 + 白名单校验")
    import tempfile

    from src.config import load_config
    from src.control import ControlError
    from src.dashboard.service import DashboardService

    with tempfile.TemporaryDirectory() as td:
        cfg = load_config()
        cfg._data["app"]["control_file"] = str(Path(td) / "control.json")
        cfg._data["app"]["state_file"] = str(Path(td) / "state.json")
        cfg._data["app"]["alert_history_file"] = str(Path(td) / "alerts.jsonl")
        svc = DashboardService(cfg, ttl_seconds=5)

        snap = svc.control_snapshot()
        check("控制快照含四类字段",
              {"symbols", "dnd", "priority", "tracks"}.issubset(set(snap.keys())),
              str(sorted(snap.keys())))
        check("轨道清单来自后端白名单",
              {t["key"] for t in snap["tracks"]}
              == {"trend", "event", "world", "short", "session"})

        inst_id = cfg.symbols[0]["inst_id"]
        out = svc.set_symbol_enabled(inst_id, False)
        row = next(s for s in out["symbols"] if s["inst_id"] == inst_id)
        check("写入品种开关后立即反映", row["enabled"] is False and row["override"] is False,
              str(row))
        try:
            svc.set_symbol_enabled("NOPE-USDT-SWAP", False)
            check("服务层拒绝未知品种", False, "没有报错")
        except ControlError:
            check("服务层拒绝未知品种", True)

        out2 = svc.set_dnd(True, [{"name": "夜间", "start": "23:00", "end": "07:00"}])
        check("写入免打扰时段", bool(out2["dnd"]["windows"]) and out2["dnd"]["enabled"],
              str(out2["dnd"]))


def test_snapshot_schema() -> None:
    print("\n[面板] 快照字段齐全、取不到数时为 null、建议话术固定")
    import tempfile

    from src.config import load_config
    from src.dashboard.service import DashboardService
    from src.datasource.okx import HttpError, Ticker

    class UpOkx:
        """行情可用：返回真实结构的数据。"""

        def get_ticker(self, inst_id: str) -> Ticker:
            return Ticker(inst_id=inst_id, last=74.23, open_24h=75.0, high_24h=76.0,
                          low_24h=73.0, vol_24h=1000.0, ts_ms=1792051721469)

        def get_candles(self, inst_id: str, bar: str = "5m", limit: int = 300) -> list:
            return [Candle(ts_ms=1792051500000, open=1.0, high=2.0, low=0.5,
                           close=1.5, volume=10.0, confirmed=True)]

        def get_weekly_candles(self, inst_id: str, bar: str = "1W", limit: int = 300) -> list:
            return self.get_candles(inst_id, bar, limit)

    class DownOkx:
        """上游整体失败：所有取数都抛 HttpError。"""

        def get_ticker(self, inst_id: str):
            raise HttpError("模拟上游失败")

        def get_candles(self, inst_id: str, bar: str = "5m", limit: int = 300):
            raise HttpError("模拟上游失败")

        def get_weekly_candles(self, inst_id: str, bar: str = "1W", limit: int = 300):
            raise HttpError("模拟上游失败")

    with tempfile.TemporaryDirectory() as tmp:
        cfg = load_config()
        cfg._data["app"]["state_file"] = str(Path(tmp) / "state.json")
        cfg._data["app"]["alert_history_file"] = str(Path(tmp) / "alerts.jsonl")
        inst_id = cfg.symbols[0]["inst_id"]

        service = DashboardService(cfg, ttl_seconds=5)
        service.okx = UpOkx()  # type: ignore[assignment]
        snap = service.snapshot(inst_id)
        expected = {"inst_id", "name", "short_name", "enabled", "price", "ts_ms", "ts_text",
                    "high_24h", "low_24h", "vol_24h", "support", "resistance", "supports",
                    "resistances", "lookback_weeks", "trend", "action_text", "notes"}
        check("快照字段齐全", expected.issubset(set(snap.keys())),
              str(sorted(expected - set(snap.keys()))))
        check("现价来自真实取数", snap["price"] == 74.23, str(snap["price"]))
        check("建议动作是固定话术",
              snap["action_text"] == "建议人工查看/等待确认", snap["action_text"])
        check("没有趋势状态记录时不编造", snap["trend"] is None, str(snap["trend"]))

        kline = service.kline(inst_id, "5m", 10)
        check("K线根数与标签数一致",
              bool(kline) and kline["count"] == 1 and len(kline["labels"]) == 1, str(kline))
        check("K线标签是北京时间", kline["labels"][0] == "2026-10-15 16:05", kline["labels"][0])

        broken = DashboardService(cfg, ttl_seconds=5)
        broken.okx = DownOkx()  # type: ignore[assignment]
        down = broken.snapshot(inst_id)
        check("上游失败时现价是 null（不拿旧值冒充）", down["price"] is None, str(down["price"]))
        check("上游失败时如实说明原因",
              any("失败" in note for note in down["notes"]), str(down["notes"]))
        check("上游失败时 K线明确报失败（不是空图伪装成功）",
              broken.kline(inst_id, "5m", 10) is None)


# ---------------------------------------------------------------- 事件结果：确定性方向
def test_event_verdict_direction() -> None:
    print("\n[事件方向] 每条公布后结果必须给明确偏多/偏空，给不出就不给推送")
    from src.modules.event import (
        branch_from_previous, verdict_for_event,
    )
    from src.datasource.econ_calendar import EconEvent

    TQQQ = "TQQQ-USDT-SWAP"
    CL = "CL-USDT-SWAP"

    v = verdict_for_event("美国9月CPI年率", TQQQ, "A", "美国")
    check("CPI 强于预期 -> 纳指【偏空】", v and v["verdict_text"] == "偏空", str(v))
    v = verdict_for_event("美国9月CPI年率", TQQQ, "B", "美国")
    check("CPI 弱于预期 -> 纳指【偏多】", v and v["verdict_text"] == "偏多", str(v))

    v = verdict_for_event("美国至当周初请失业金人数", TQQQ, "A", "美国")
    check("初请失业金走高（反向指标）-> 纳指【偏多】",
          v and v["verdict_text"] == "偏多", str(v))
    v = verdict_for_event("美国至当周初请失业金人数", TQQQ, "B", "美国")
    check("初请失业金走低（反向指标）-> 纳指【偏空】",
          v and v["verdict_text"] == "偏空", str(v))

    v = verdict_for_event("美国至当周EIA原油库存", CL, "A", "美国")
    check("EIA 原油库存增加 -> 原油【偏空】",
          v and v["verdict_text"] == "偏空", str(v))
    v = verdict_for_event("美国至当周EIA原油库存", CL, "B", "美国")
    check("EIA 原油库存下降 -> 原油【偏多】",
          v and v["verdict_text"] == "偏多", str(v))

    check("认不出类别的事件 -> verdict 为 None（不硬猜）",
          verdict_for_event("某国际组织年度峰会公报", TQQQ, "A", "其他") is None)
    check("选举类事件没有固定口径 -> None",
          verdict_for_event("某国大选首场辩论", "UNKNOWN-X", "A") is None)

    ev = EconEvent(date="2026-10-15", time="20:30", region="美国",
                   name="美国9月CPI年率", actual=3.7, forecast=3.7, previous=3.4)
    check("C（与预期持平）但高于前值 -> 归并为 A", branch_from_previous(ev) == "A")
    ev2 = EconEvent(date="2026-10-15", time="20:30", region="美国",
                    name="美国9月CPI年率", actual=3.7, forecast=3.7, previous=None)
    check("C 且没有前值 -> 归并不了（None，不推）", branch_from_previous(ev2) is None)
    ev3 = EconEvent(date="2026-10-15", time="20:30", region="美国",
                    name="美国9月CPI年率", actual=3.7, forecast=3.7, previous=3.7)
    check("与预期、前值都持平 -> None（没有数据依据给方向）",
          branch_from_previous(ev3) is None)

    v = verdict_for_event("欧元区就业人口", TQQQ, "A", "欧元区")
    check("非美事件传导链不写「美联储」", v and "美联储" not in v["chain"]
          and "该国央行" in v["chain"], str(v and v["chain"]))
    v = verdict_for_event("美国至当周EIA原油库存", CL, "A", "美国")
    check("原油库存链保留自己的口径（不替换央行字样）",
          v and "库存增加" in v["chain"], str(v and v["chain"]))


def test_event_post_verdict_push() -> None:
    print("\n[事件方向] 给不出方向时整条不推；给得出时标题/正文必须带方向")
    import tempfile
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from run import PUSH_SKIPPED
    from src.datasource.econ_calendar import BEIJING, EconEvent
    from src.modules.levels import Levels

    levels_dict = Levels("TQQQ-USDT-SWAP", 80.0, supports=[78.0],
                         resistances=[82.0]).as_dict()
    now = datetime.now(BEIJING)

    with tempfile.TemporaryDirectory() as td:
        m = _make_monitor(Path(td))
        m._symbol_levels = lambda symbol: (levels_dict, 80.0, "ts", 1760000000000)
        sent: list[tuple[str, str]] = []
        m.notifier.send = lambda t, b: (sent.append((t, b)),
                                        SimpleNamespace(ok=True, detail=""))[1]

        generic = EconEvent(date=now.strftime("%Y-%m-%d"), time="20:30",
                            region="其他", name="某国际组织年度峰会公报",
                            actual=1.0, forecast=0.5, previous=0.4, importance=1)
        status = m._emit_post_result(generic, now, [], "event|generic", False)
        check("无法识别类别 -> PUSH_SKIPPED 且不推送",
              status == PUSH_SKIPPED and not sent, f"status={status} sent={len(sent)}")

        flat = EconEvent(date=now.strftime("%Y-%m-%d"), time="20:30",
                         region="美国", name="美国9月CPI年率",
                         actual=3.7, forecast=3.7, previous=None, importance=3)
        status = m._emit_post_result(flat, now, [], "event|flat", False)
        check("C 且无前值 -> PUSH_SKIPPED 且不推送",
              status == PUSH_SKIPPED and not sent, f"status={status} sent={len(sent)}")

        cpi = EconEvent(date=now.strftime("%Y-%m-%d"), time="20:30",
                        region="美国", name="美国9月CPI年率",
                        actual=3.9, forecast=3.7, previous=3.4, importance=3)
        status = m._emit_post_result(cpi, now, [], "event|cpi", False)
        check("CPI 强于预期 -> 成功推送且方向为【偏空】",
              status == "sent" and len(sent) == 1
              and "偏空" in sent[0][0] and "方向研判：偏空" in sent[0][1],
              f"{status} {sent[0][0] if sent else ''}")
        check("传导链使用历史口径措辞（不写必涨必跌）",
              "历史上通常" in sent[0][1], "")
        check("正文出现真实实际值 3.9", "3.9" in sent[0][1], "")

        no_fc = EconEvent(date=now.strftime("%Y-%m-%d"), time="20:30",
                          region="美国", name="美国9月CPI年率",
                          actual=3.9, forecast=None, previous=3.5, importance=3)
        before = len(sent)
        m._emit_post_result(no_fc, now, [], "event|nofc", False)
        body = sent[-1][1]
        check("无预期值时参照前值给分支，且不展示「数据源未提供预期值」",
              len(sent) == before + 1 and "强于前值" in sent[-1][0]
              and "数据源未提供" not in body, sent[-1][0])


# ---------------------------------------------------------------- 国际大事：纯逻辑
def test_world_logic() -> None:
    print("\n[国际大事] 关键词分类 / 初筛门槛 / 确定性兜底方向")
    from src.modules import world as W

    cat, score, _, title_hit = W.score_news("Airstrike hits capital as war escalates", "")
    check("标题命中战争词 -> war 且标题分>=3",
          cat == "war" and score >= 3 and title_hit, f"{cat} {score}")
    cat2, score2, _, title_hit2 = W.score_news(
        "Daily markets recap",
        "officials discuss tariff and sanctions options")
    check("仅摘要命中（哪怕凑够 3 分）-> title_hit=False，被标题门槛拦住",
          cat2 == "policy_trade" and score2 >= 3 and title_hit2 is False,
          f"{cat2} {score2}")
    cat3, score3, _, title_hit3 = W.score_news("Local food festival opens this weekend", "")
    check("无关新闻 -> other / 0 分",
          cat3 == "other" and score3 == 0 and title_hit3 is False,
          f"{cat3} {score3}")

    from src.datasource.world_news import NewsItem, normalize_url

    def ni(title, url, summary="", ts=1_760_000_000.0):
        return NewsItem(title=title, url=url, source="BBC", published_ts=ts,
                        summary=summary, via="rss:BBC")

    raw = [
        ni("Airstrike hits capital as war escalates", "https://a.com/x"),
        # 数据源层已按 normalize_url 去重，这里再放一条完全同 URL 验证初筛自身去重
        ni("Airstrike hits capital as war escalates", "https://a.com/x", ts=1_760_000_001.0),
        ni("Markets weekly recap",
           "https://a.com/weekly",
           "mentions tariff pressure on supply chains"),  # 仅摘要
        ni("OPEC pipeline attack raises oil supply fears", "https://a.com/oil"),
        ni("Election candidates hold final debate", "https://a.com/vote"),
        ni("Food festival draws crowds", "https://a.com/food"),
    ]
    cands = W.filter_candidates(raw, seen_urls=set(), min_score=2,
                                require_title_hit=True, limit=12)
    urls = {c.url for c in cands}
    check("标题命中的才入选（摘要蹭词/无关新闻剔除）",
          "https://a.com/x" in urls and "https://a.com/oil" in urls
          and "https://a.com/vote" in urls
          and "https://a.com/weekly" not in urls
          and "https://a.com/food" not in urls, str(urls))
    check("同 URL 只保留一条",
          sum(1 for c in cands if c.url == "https://a.com/x") == 1)
    check("跟踪参数/fragment 规范化后等价（数据源层按它去重）",
          normalize_url("https://a.com/x?utm_source=z#frag") == normalize_url("https://a.com/x"))

    check("战争 -> 纳指偏空（避险）",
          W.fallback_bias("war", "TQQQ-USDT-SWAP") == "偏空")
    check("战争 -> 原油偏多（供应担忧）",
          W.fallback_bias("war", "CL-USDT-SWAP") == "偏多")
    check("央行宽松 -> 纳指偏多",
          W.fallback_bias("cb_ease", "TQQQ-USDT-SWAP") == "偏多")
    check("央行收紧 -> 纳指偏空",
          W.fallback_bias("cb_tighten", "TQQQ-USDT-SWAP") == "偏空")
    check("技术突破 -> 纳指偏多",
          W.fallback_bias("tech_breakthrough", "TQQQ-USDT-SWAP") == "偏多")
    check("选举方向取决于人选 -> None（宁可不推）",
          W.fallback_bias("election", "TQQQ-USDT-SWAP") is None)
    check("弱相关组合（技术突破 vs 原油）-> None",
          W.fallback_bias("tech_breakthrough", "CL-USDT-SWAP") is None)
    war_cand = next(c for c in cands if c.category == "war")
    check("兜底正文必须原样引用真实标题 + 人工确认话术",
          "Airstrike" in W.fallback_body(war_cand, "TQQQ-USDT-SWAP", "偏空")
          and "人工查看" in W.fallback_body(war_cand, "TQQQ-USDT-SWAP", "偏空"))

    check("normalize_url 去 utm/fragment/小写域名/尾斜杠",
          normalize_url("https://A.COM/X/?utm_source=n#frag") == "https://a.com/X")


def test_world_verdicts_parse() -> None:
    print("\n[国际大事] 大模型研判 JSON 校验：URL/数字/方向值任何一项造假即丢弃")
    import json
    from src.datasource.world_news import NewsItem
    from src.modules import world as W

    def ni(title, url):
        return NewsItem(title=title, url=url, source="BBC",
                        published_ts=1_760_000_000.0, summary="", via="gdelt")

    items = [
        ni("Airstrike hits capital as war escalates", "https://a.com/war"),
        ni("Port blockade disrupts crude oil shipments", "https://a.com/oil"),
        ni("Election candidates hold final debate", "https://a.com/vote"),
    ]
    cands = W.filter_candidates(items, seen_urls=set())
    ids = {"TQQQ-USDT-SWAP", "CL-USDT-SWAP"}

    good = [{
        "url": "https://a.com/war", "major": True, "category": "war",
        "summary_zh": "首都遭空袭，地缘冲突升级",
        "chain": "避险情绪升温 → 历史上通常压制风险资产、利多原油",
        "bias": {"TQQQ-USDT-SWAP": "偏空", "CL-USDT-SWAP": "偏多"},
        "confidence": 2,
    }]
    verdicts = W.parse_world_verdicts(json.dumps(good), cands, ids)
    v_good = verdicts.get("https://a.com/war", {})
    check("合规 JSON -> 采纳且两个品种方向都保留",
          v_good.get("major") is True
          and v_good.get("bias", {}).get("TQQQ-USDT-SWAP") == "偏空"
          and v_good.get("bias", {}).get("CL-USDT-SWAP") == "偏多",
          str(verdicts))
    check("没点评到的 URL 不会凭空出现结果",
          "https://a.com/vote" not in verdicts, str(verdicts))

    # 编造数字：原文没有 5000
    fake = [dict(good[0], url="https://a.com/oil",
                 summary_zh="封锁造成 5000 桶原油外运中断，供应担忧升温")]
    verdicts2 = W.parse_world_verdicts(json.dumps(fake), cands, ids)
    check("摘要里出现原文没有的数字 -> 该条丢弃（走兜底）",
          "https://a.com/oil" not in verdicts2, str(verdicts2))

    bad_url = [dict(good[0], url="https://evil.com/not-in-input")]
    verdicts3 = W.parse_world_verdicts(json.dumps(bad_url), cands, ids)
    check("URL 对不上输入 -> 丢弃", verdicts3 == {}, str(verdicts3))

    bad_bias = [dict(good[0], bias={"FAKE-USDT-SWAP": "偏空",
                                    "TQQQ-USDT-SWAP": "暴涨"})]
    verdicts4 = W.parse_world_verdicts(json.dumps(bad_bias), cands, ids)
    check("非法品种代码/非法方向值全部剔除 -> bias 为空导致条目丢弃",
          "https://a.com/war" not in verdicts4, str(verdicts4))

    neutral = [dict(good[0], confidence=0)]
    verdicts5 = W.parse_world_verdicts(json.dumps(neutral), cands, ids)
    check("confidence<1 -> 标记为非大事",
          verdicts5.get("https://a.com/war") == {"major": False}, str(verdicts5))

    neutral2 = [dict(good[0], bias={"TQQQ-USDT-SWAP": "中性"})]
    verdicts6 = W.parse_world_verdicts(json.dumps(neutral2), cands, ids)
    check("中性方向允许解析（由推送层决定该品种不推）",
          verdicts6["https://a.com/war"]["bias"]["TQQQ-USDT-SWAP"] == "中性")

    check("整体不是 JSON -> {}",
          W.parse_world_verdicts("sorry, no json", cands, ids) == {})
    check("```json fence 容忍",
          W.parse_world_verdicts("```json\n" + json.dumps(good) + "\n```",
                                 cands, ids)
          .get("https://a.com/war", {}).get("major") is True)

    # 思考段落：合规保留；出现原文没有的数字时只丢这一段，不拖累整条
    with_think = [dict(good[0], thinking="这条消息已经是既成事实，冲突有升级迹象；"
                                          "我判偏空的主要依据是避险情绪升温；"
                                          "最不确定的是会不会有后续报复行动。")]
    v_think = W.parse_world_verdicts(json.dumps(with_think), cands, ids)
    check("合规思考段落被保留",
          "既成事实" in v_think.get("https://a.com/war", {}).get("thinking", ""),
          str(v_think))
    dirty_think = [dict(good[0], thinking="袭击造成 1200 人死亡，因此偏空。")]
    v_dirty = W.parse_world_verdicts(json.dumps(dirty_think), cands, ids)
    v_dirty_item = v_dirty.get("https://a.com/war", {})
    check("思考段落编造数字 -> 只丢思考，事实与方向仍保留",
          v_dirty_item.get("major") is True and v_dirty_item.get("thinking") == ""
          and v_dirty_item.get("bias", {}).get("TQQQ-USDT-SWAP") == "偏空",
          str(v_dirty))


def test_world_news_feed_parse() -> None:
    print("\n[国际大事] RSS 2.0 / Atom 解析与时间换算")
    from src.datasource.world_news import WorldNewsClient

    client = WorldNewsClient(rss_feeds=[])
    rss = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>BBC World</title>
<item><title>Airstrike hits border region</title>
<link>https://news.test/a</link>
<pubDate>Wed, 15 Oct 2025 10:00:00 GMT</pubDate>
<description>Reports of a military strike overnight.</description></item>
<item><title></title><link>https://news.test/bad</link></item>
</channel></rss>"""
    items = client.parse_feed_bytes(rss, "https://news.test/feed")
    check("RSS 解析出 1 条有效 item（坏条目跳过）", len(items) == 1, str(len(items)))
    check("标题/链接/来源/via 正确",
          items[0].title == "Airstrike hits border region"
          and items[0].url == "https://news.test/a"
          and items[0].via.startswith("rss:"), str(items[0]))
    check("RFC822 时间被解析", items[0].published_ts is not None)
    check("发布时间转成北京时间字符串",
          items[0].published_text.endswith("北京时间") and "18:00" in items[0].published_text,
          items[0].published_text)
    check("description 剥 HTML/压空白", items[0].summary.startswith("Reports"),
          items[0].summary)

    atom = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<entry><title>Ceasefire announced in war zone</title>
<link href="https://atom.test/c"/>
<published>2025-10-15T09:00:00Z</published>
<summary>Parties agree to halt military operations.</summary></entry></feed>"""
    atoms = client.parse_feed_bytes(atom, "")
    check("Atom 解析出 1 条 entry",
          len(atoms) == 1 and atoms[0].url == "https://atom.test/c"
          and atoms[0].published_ts is not None, str(atoms))

    empty = client.parse_feed_bytes(b"<rss><channel></channel></rss>", "")
    check("没有 item 的 feed -> 空列表", empty == [])

    import requests as _rq

    def _boom(*a, **k):
        raise _rq.RequestException("测试环境无网络")
    client.session.get = _boom
    check("取数异常不炸调用方（fetch_rss 内部吞掉异常返回空）",
          client.fetch_rss("https://offline.test/feed") == [])


def test_world_state_memory() -> None:
    print("\n[国际大事] state 去重记忆 / 每日上限 / 跨天清零")
    import tempfile
    from src.state import StateStore

    with tempfile.TemporaryDirectory() as td:
        store = StateStore(Path(td) / "s.json")
        url = "https://a.com/x"
        check("新 URL 未评估", store.is_world_evaluated(url) is False)
        check("该品种未推送过", store.world_symbol_done(url, "TQQQ-USDT-SWAP") is False)
        store.mark_world_symbol_sent(url, "TQQQ-USDT-SWAP")
        check("推送后该品种标记完成", store.world_symbol_done(url, "TQQQ-USDT-SWAP"))
        check("不同品种互不影响",
              store.world_symbol_done(url, "CL-USDT-SWAP") is False)
        check("当日计数 +1", store.bump_world_pushed("2026-10-15") == 1)
        check("当日计数 +1 -> 2", store.bump_world_pushed("2026-10-15") == 2)
        # 只读不 bump：不能让查「明天计数」顺手把今天的清了
        check("次日计数从 0 开始", store.world_pushed_today("2026-10-16") == 0)
        check("查询不影响今天的计数", store.world_pushed_today("2026-10-15") == 2)
        store.mark_world_evaluated(url)
        check("标记已评估", store.is_world_evaluated(url))
        store.save()
        again = StateStore(Path(td) / "s.json")
        check("记忆能落盘读回",
              again.world_symbol_done(url, "TQQQ-USDT-SWAP")
              and again.world_pushed_today("2026-10-15") == 2)
        check("跨天第一次计数会清掉旧日期",
              again.bump_world_pushed("2026-10-16") == 1
              and again.world_pushed_today("2026-10-15") == 0)


def test_world_alert_render() -> None:
    print("\n[国际大事] 推送版式：方向置顶、来源/链接/固定话术齐全")
    from src.alert import WorldAlert

    alert = WorldAlert(
        happened="首都遭空袭，冲突升级",
        category="war", verdict="偏空",
        verdict_chain="避险情绪升温 → 历史上通常压制风险资产",
        body="请人工查看事件进展与价格确认。",
        source="BBC", url="https://a.com/x",
        published_text="2026-10-15 18:00 北京时间",
        symbol="纳指三倍做多（TQQQ-USDT-SWAP）", short_name="TQQQ",
        price=80.12, action_text="建议人工查看/等待确认",
        disclaimer="以上为历史规律梳理，仅供参考，决策与风险自负",
        inst_id="TQQQ-USDT-SWAP", ts_ms=1760000000000,
    )
    title, body = alert.title(), alert.render()
    check("标题直接给方向【偏空】", "国际大事：偏空" in title, title)
    check("正文首屏带方向研判", "方向研判：偏空" in body, "")
    check("带来源与原文链接", "BBC" in body and "https://a.com/x" in body, "")
    check("现价明确标注仅记录、非触发价", "非触发价" in body, "")
    check("固定动作话术", "建议人工查看/等待确认" in body, "")
    check("没有买卖措辞", not any(w in body for w in
          ("建议买入", "建议卖出", "必涨", "必跌", "抄底")), "")

    # 每条推送都要有一段「AI 思考」，并且思考段落不能被别的段落吞掉
    with_think = WorldAlert(
        happened="首都遭空袭，冲突升级", category="war", verdict="偏空",
        verdict_chain="避险情绪升温 → 历史上通常压制风险资产",
        body="传导链摘要", thinking="这条消息还在演进，判偏空的依据是避险升温。",
        source="BBC", url="https://a.com/x", published_text="2026-10-15 18:00 北京时间",
        short_name="TQQQ", action_text="建议人工查看/等待确认",
        disclaimer="以上为历史规律梳理，仅供参考，决策与风险自负",
    )
    think_body = with_think.render()
    check("思考段落用「### 🤖 AI 思考」当小标题",
          "### 🤖 AI 思考" in think_body, think_body)
    check("渲染的是思考本身（不是 body）",
          "这条消息还在演进" in think_body, think_body)
    # 大模型不可用时：标题改口径，不能让规则模板冒充 AI 写的
    rule_only = WorldAlert(
        happened="首都遭空袭，冲突升级", category="war", verdict="偏空",
        verdict_chain="避险情绪升温 → 历史上通常压制风险资产",
        body="规则模板文案", source="BBC", url="https://a.com/x",
        published_text="2026-10-15 18:00 北京时间", short_name="TQQQ",
        action_text="建议人工查看/等待确认",
        disclaimer="以上为历史规律梳理，仅供参考，决策与风险自负",
        rule_text=True,
    )
    rule_body = rule_only.render()
    check("兜底文案不冒充 AI 思考",
          "### 📝 规则文案" in rule_body and "AI 思考" not in rule_body, rule_body)


def test_world_track_monitor() -> None:
    print("\n[国际大事] 端到端：取数->兜底研判->推送->去重->上限->中性不推")
    import json
    import tempfile
    from types import SimpleNamespace

    from src.datasource.okx import Ticker
    from src.datasource.world_news import NewsItem

    def ni(title, url, ts=1_760_000_000.0):
        return NewsItem(title=title, url=url, source="BBC World",
                        published_ts=ts, summary="", via="rss:BBC World")

    def new_monitor(td):
        m = _make_monitor(Path(td))
        m.world_watch = ["TQQQ-USDT-SWAP"]
        m.okx.get_ticker = lambda i: Ticker(
            i, 80.0, 0, 0, 0, 0, int(time.time() * 1000))
        sent: list[tuple[str, str]] = []
        m.notifier.send = lambda t, b: (sent.append((t, b)),
                                        SimpleNamespace(ok=True, detail=""))[1]
        return m, sent

    # 1) 大模型不可用 -> 确定性兜底：战争推偏空，选举不推，无关新闻不推
    with tempfile.TemporaryDirectory() as td:
        m, sent = new_monitor(td)
        items = [
            ni("Airstrike hits capital as war escalates", "https://a.com/war"),
            ni("Election candidates hold final debate", "https://a.com/vote"),
            ni("Local food festival opens this weekend", "https://a.com/food"),
        ]
        m.world_news.fetch_all = lambda: (items, "rss:test")

        def boom(sp, um, **kw):
            raise RuntimeError("测试环境无大模型")
        m.llm.chat = boom

        pushed = m.run_world_events(force=True)
        check("兜底路径只推 1 条（战争），选举/无关新闻不推",
              len(pushed) == 1 and len(sent) == 1, f"{pushed}")
        check("战争对纳指给【偏空】且方向在标题里",
              "偏空" in sent[0][0], sent[0][0])
        check("正文带原文链接与来源",
              "https://a.com/war" in sent[0][1] and "BBC" in sent[0][1], "")
        check("大模型不可用时该段标注为规则文案（不冒充 AI 思考）",
              "### 📝 规则文案" in sent[0][1] and "AI 思考" not in sent[0][1],
              sent[0][1])
        check("当日计数为 1", m.state.world_pushed_today(
            datetime.now(BJ).strftime("%Y-%m-%d")) == 1)

        pushed2 = m.run_world_events(force=True)
        check("第二轮不重复推送同一条新闻", pushed2 == [] and len(sent) == 1,
              f"{pushed2} {len(sent)}")

    # 2) 单轮上限：两条可推只推 1 条，另一条下一轮再推
    with tempfile.TemporaryDirectory() as td:
        m, sent = new_monitor(td)
        m.world_per_run = 1
        m.world_per_day = 10
        items = [
            ni("Airstrike hits capital as war escalates", "https://a.com/war"),
            ni("OPEC pipeline attack raises oil supply fears", "https://a.com/oil"),
        ]
        m.world_news.fetch_all = lambda: (items, "rss:test")
        m.llm.chat = boom
        first = m.run_world_events(force=True)
        check("单轮上限 1 -> 本轮只推 1 条", len(first) == 1, str(first))
        second = m.run_world_events(force=True)
        check("下一轮把另一条推完", len(second) == 1 and len(sent) == 2,
              f"{second} total={len(sent)}")

    # 3) 大模型判中性 -> 该品种不推（且不允许编造数字蒙混）
    with tempfile.TemporaryDirectory() as td:
        m, sent = new_monitor(td)
        items = [ni("Airstrike hits capital as war escalates", "https://a.com/war")]
        m.world_news.fetch_all = lambda: (items, "rss:test")
        reply = json.dumps([{
            "url": "https://a.com/war", "major": True, "category": "war",
            "summary_zh": "首都遭空袭，冲突升级",
            "chain": "避险情绪升温",
            "bias": {"TQQQ-USDT-SWAP": "中性"},
            "confidence": 2,
        }])
        m.llm.api_key = "test-key"  # 强制走大模型分支（本机没配 key 也能测）
        m.llm.chat = lambda sp, um, **kw: reply
        pushed = m.run_world_events(force=True)
        check("大模型判中性 -> 不推送", pushed == [] and sent == [], str(pushed))
        check("中性新闻已标记评估，不会每轮重复问大模型",
              m.state.is_world_evaluated("https://a.com/war"))

    # 4) 跨轨口径提示：同品种近期有相反方向的做空观察位 -> 国际大事正文带提示
    with tempfile.TemporaryDirectory() as td:
        m, sent = new_monitor(td)
        m.world_watch = ["CL-USDT-SWAP"]
        items = [ni("OPEC pipeline attack raises oil supply fears", "https://a.com/oil")]
        m.world_news.fetch_all = lambda: (items, "rss:test")
        m.llm.chat = boom
        m._note_direction("CL-USDT-SWAP", "偏空", SIGNAL_SHORT)
        pushed = m.run_world_events(force=True)
        check("国际大事偏多 + 近期做空观察位 -> 正文带跨轨口径提示",
              len(sent) == 1 and "跨轨口径提示" in sent[0][1],
              (sent[0][1][-220:] if sent else str(pushed)))
        check("提示同时点出两条轨",
              bool(sent) and "做空观察位" in sent[0][1] and "国际大事" in sent[0][1])


def main() -> int:
    print("=" * 66)
    print("双轨信号监控系统 —— 安全自测")
    print("=" * 66)
    test_number_guard()
    test_forbidden_words()
    test_disclaimer()
    test_trend_insufficient_data()
    test_trigger_wording()
    test_branch_determination()
    test_position_pnl()
    test_levels()
    test_session_watch()
    test_session_window_reset()
    test_push_failure_not_lost()
    test_event_post_result()
    test_event_verdict_direction()
    test_event_post_verdict_push()
    test_world_logic()
    test_world_verdicts_parse()
    test_world_news_feed_parse()
    test_world_state_memory()
    test_world_alert_render()
    test_world_track_monitor()
    test_calendar_lookback()
    test_caching()
    test_calendar_columns()
    test_intraday_levels()
    test_orderflow_metrics()
    test_short_scoring()
    test_short_alert_wording()
    test_fake_break_followup()
    test_cross_track_conflict_note()
    test_dry_run_writes_nothing()
    test_alert_layout()
    test_multi_sendkey()
    test_formatting()
    test_alert_history()
    test_alert_record_from_alert()
    test_bar_alignment()
    test_dashboard_readonly()
    test_strategy_isolation()
    test_control_store()
    test_control_dnd_defer()
    test_control_position_override()
    test_control_service()
    test_snapshot_schema()
    print("\n" + "=" * 66)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
