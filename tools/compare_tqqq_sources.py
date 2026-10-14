"""对比「美股本土 TQQQ」与「OKX TQQQ 永续」的走势是否一致。

回答一个问题：能不能直接用美股本土历史数据来补 OKX 周K历史不足的缺口？
如果两者走势不一致，混用会把均线算歪，那就不能用。

运行：python tools/compare_tqqq_sources.py
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from src.datasource.okx import OkxClient

UTC8 = timezone(timedelta(hours=8))
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/TQQQ"


def fetch_yahoo_daily(symbol: str = "TQQQ", rng: str = "10y") -> list[dict]:
    resp = requests.get(
        YAHOO.replace("TQQQ", symbol),
        params={"range": rng, "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    resp.raise_for_status()
    res = resp.json()["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        if q["close"][i] is None:
            continue
        rows.append({
            "ts": t,
            "date": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
            "open": q["open"][i], "high": q["high"][i],
            "low": q["low"][i], "close": q["close"][i],
            "volume": q["volume"][i],
        })
    return rows


def to_weekly_us(daily: list[dict]) -> list[dict]:
    """把日线聚合成「美股交易周」（周一~周五），以周一日期为标签。"""
    buckets: dict[str, list[dict]] = {}
    for row in daily:
        d = datetime.strptime(row["date"], "%Y-%m-%d").date()
        monday = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
        buckets.setdefault(monday, []).append(row)
    weekly = []
    for monday in sorted(buckets):
        rows = sorted(buckets[monday], key=lambda r: r["date"])
        weekly.append({
            "week": monday,
            "open": rows[0]["open"],
            "high": max(r["high"] for r in rows),
            "low": min(r["low"] for r in rows),
            "close": rows[-1]["close"],
            "days": len(rows),
        })
    return weekly


def to_weekly_okx(candles) -> list[dict]:
    """OKX 周K：bar 起点是周日 16:00 UTC（= 周一 00:00 北京时间），用它当周标签。"""
    out = []
    for c in candles:
        if not c.confirmed:
            continue
        dt8 = c.dt_utc.astimezone(UTC8)
        out.append({
            "week": dt8.strftime("%Y-%m-%d"),
            "open": c.open, "high": c.high, "low": c.low, "close": c.close,
        })
    return out


def sma(values: list[float], period: int) -> float | None:
    return sum(values[-period:]) / period if len(values) >= period else None


def trend_state(closes: list[float], period: int, tol_pct: float = 0.3) -> str | None:
    ma_now = sma(closes, period)
    ma_prev = sma(closes[:-1], period)
    if ma_now is None or ma_prev is None:
        return None
    tol = tol_pct / 100.0
    above = closes[-1] > ma_now * (1 + tol)
    below = closes[-1] < ma_now * (1 - tol)
    if above and ma_now > ma_prev:
        return "bull"
    if below and ma_now < ma_prev:
        return "bear"
    return "range"


def main() -> int:
    print("=" * 78)
    print("美股本土 TQQQ  vs  OKX TQQQ-USDT-SWAP   走势一致性核查")
    print("=" * 78)

    daily = fetch_yahoo_daily("TQQQ", "10y")
    us_weekly = to_weekly_us(daily)
    print(f"\n[美股东土] Yahoo TQQQ 日线 {len(daily)} 根 -> 周线 {len(us_weekly)} 根")
    print(f"           区间 {us_weekly[0]['week']} ~ {us_weekly[-1]['week']}")
    print(f"           最新收盘 {us_weekly[-1]['close']:.2f}")

    okx = OkxClient()
    candles = okx.get_weekly_candles("TQQQ-USDT-SWAP", "1W", 300)
    okx_weekly = to_weekly_okx(candles)
    print(f"\n[OKX 永续] 周线 {len(okx_weekly)} 根（已收盘）")
    print(f"           区间 {okx_weekly[0]['week']} ~ {okx_weekly[-1]['week']}")
    print(f"           最新收盘 {okx_weekly[-1]['close']:.2f}")

    # ---- 按周对齐 ----
    us_map = {w["week"]: w for w in us_weekly}
    okx_map = {w["week"]: w for w in okx_weekly}
    common = sorted(set(us_map) & set(okx_map))
    print(f"\n[重叠区间] 共 {len(common)} 个自然周可比对")
    if len(common) < 5:
        print("重叠周太少，无法给出可靠结论")
        return 1

    print("\n" + "-" * 78)
    print("逐周明细（最近 16 周）")
    print("-" * 78)
    print(f"{'周起始(周一)':<14}{'美股收盘':>10}{'OKX收盘':>11}{'价差%':>9}"
          f"{'美股周涨跌%':>12}{'OKX周涨跌%':>12}")
    for i, week in enumerate(common):
        u, o = us_map[week], okx_map[week]
        diff = (o["close"] - u["close"]) / u["close"] * 100.0
        u_ret = o_ret = float("nan")
        if i > 0:
            pw = common[i - 1]
            u_ret = (u["close"] / us_map[pw]["close"] - 1) * 100.0
            o_ret = (o["close"] / okx_map[pw]["close"] - 1) * 100.0
        if i >= len(common) - 16:
            u_s = f"{u_ret:+.2f}" if i > 0 else "-"
            o_s = f"{o_ret:+.2f}" if i > 0 else "-"
            print(f"{week:<14}{u['close']:>10.2f}{o['close']:>11.2f}{diff:>+9.2f}{u_s:>12}{o_s:>12}")

    # ---- 统计 ----
    diffs = [(okx_map[w]["close"] - us_map[w]["close"]) / us_map[w]["close"] * 100 for w in common]
    us_rets, okx_rets = [], []
    for i in range(1, len(common)):
        pw, w = common[i - 1], common[i]
        us_rets.append(us_map[w]["close"] / us_map[pw]["close"] - 1)
        okx_rets.append(okx_map[w]["close"] / okx_map[pw]["close"] - 1)

    mean_diff = statistics.mean(diffs)
    max_abs_diff = max(abs(d) for d in diffs)
    corr = statistics.correlation(us_rets, okx_rets)
    same_dir = sum(1 for a, b in zip(us_rets, okx_rets) if (a > 0) == (b > 0))
    same_dir_pct = same_dir / len(us_rets) * 100
    max_ret_gap = max(abs(a - b) for a, b in zip(us_rets, okx_rets))
    mean_ret_gap = statistics.mean(abs(a - b) for a, b in zip(us_rets, okx_rets))

    print("\n" + "=" * 78)
    print("统计结论")
    print("=" * 78)
    print(f"价格水平   : 平均价差 {mean_diff:+.3f}%   最大价差 {max_abs_diff:.3f}%")
    print(f"周收益相关 : {corr:.4f}")
    print(f"同向周占比 : {same_dir}/{len(us_rets)} = {same_dir_pct:.1f}%")
    print(f"周收益偏差 : 平均 {mean_ret_gap*100:.3f} 个百分点   最大 {max_ret_gap*100:.3f} 个百分点")

    # ---- 趋势状态是否一致 ----
    print("\n" + "-" * 78)
    print("如果分别用两套数据算周线趋势状态，结论会不会一样？")
    print("-" * 78)
    agree_all = True
    for period in (20, 30):
        us_states, okx_states, agree, total = [], [], 0, 0
        for i in range(period + 1, len(common) + 1):
            window = common[:i]
            us_closes = [us_map[w]["close"] for w in window]
            okx_closes = [okx_map[w]["close"] for w in window]
            su = trend_state(us_closes, period)
            so = trend_state(okx_closes, period)
            if su and so:
                total += 1
                if su == so:
                    agree += 1
        if total:
            pct = agree / total * 100
            print(f"MA{period}: 逐周状态一致 {agree}/{total} = {pct:.1f}%")
            if pct < 95:
                agree_all = False
        else:
            print(f"MA{period}: 重叠周不足，无法比对")
            agree_all = False

    # ---- 最终判定 ----
    print("\n" + "=" * 78)
    verdict_ok = corr > 0.95 and same_dir_pct > 90 and max_abs_diff < 5
    print("判定：" + ("两者走势高度一致，可以互相补充" if verdict_ok
                   else "两者存在明显差异，混用有风险，需要人工确认"))
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
