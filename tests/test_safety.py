"""安全自测：证明红线条款在代码层面真的生效。

不依赖 pytest，直接运行：
    python tests/test_safety.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alert import SafetyFilter, compute_position_pnl, fmt_num
from src.datasource.okx import Candle
from src.llm.deepseek import find_fabricated_numbers
from src.modules.event import determine_branch, render_fallback_script
from src.modules.trend import evaluate_trend

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
        last_price=80.81,
    )
    check("能算出盈亏", pnl is not None)
    if pnl:
        check("名义价值 = 保证金 x 杠杆", abs(pnl.notional - 400.0) < 1e-6, str(pnl.notional))
        check("浮亏为负", pnl.unrealized < 0, str(pnl.unrealized))
        check("盈亏符号与方向一致（多单跌则亏）",
              abs(pnl.unrealized - (80.81 - 100.0) * pnl.qty) < 1e-6)

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
    lines = text.splitlines()

    check("短名从 inst_id 提取", a.short_name == "TQQQ", a.short_name)
    check("第一行是品种", lines[0].startswith("【品种】"), lines[0])
    check("第二行是发生了什么", lines[1].startswith("【发生了什么】"), lines[1])
    check("第三行是时间", lines[2].startswith("【时间】"), lines[2])
    check("第四行是当前位置", lines[3].startswith("【当前位置】"), lines[3])
    check("第五行是压力位", lines[4].startswith("【压力位】"), lines[4])
    check("第六行是支撑位", lines[5].startswith("【支撑位】"), lines[5])
    check("第七行是当下该做什么", lines[6].startswith("【当下该做什么】"), lines[6])
    check("风险提示在最后一行",
          lines[-1].startswith("【风险提示】") and a.disclaimer in lines[-1], lines[-1])

    check("压力位标注在上方", "上方" in lines[4], lines[4])
    check("支撑位标注在下方", "下方" in lines[5], lines[5])
    check("压力位距离计算正确", "2.97%" in lines[4], lines[4])
    check("支撑位距离计算正确", "1.81%" in lines[5], lines[5])

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
    test_alert_layout()
    test_formatting()
    print("\n" + "=" * 66)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
