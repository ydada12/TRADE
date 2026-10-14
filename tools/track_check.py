"""深度核查：OKX TQQQ 永续 是否真实跟踪美股 TQQQ。

方法：用「按日期对齐的日线」比较，避免周线口径差异（OKX 周K收在周日 16:00 UTC，
会把周末加密盘的波动算进去；美股周K收在周五）。

运行：python tools/track_check.py
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/TQQQ"
OKX_BASE = "https://www.okx.com"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_yahoo_daily(range_: str = "2y") -> dict[str, float]:
    resp = requests.get(
        YAHOO, params={"range": range_, "interval": "1d"}, headers=HEADERS, timeout=30
    )
    resp.raise_for_status()
    res = resp.json()["chart"]["result"][0]
    ts, q = res["timestamp"], res["indicators"]["quote"][0]
    out: dict[str, float] = {}
    for i, t in enumerate(ts):
        if q["close"][i] is None:
            continue
        out[datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")] = q["close"][i]
    return out


def fetch_okx_daily_utc(inst_id: str, max_pages: int = 12) -> dict[str, float]:
    """拉 OKX 的 1Dutc 日线（UTC 00:00 对齐），返回 {UTC日期: 收盘}。

    1Dutc 的日线收线在当日 24:00 UTC，比美股收盘（20:00 UTC）晚 4 小时，
    但已远好于周线口径的偏差。
    """
    out: dict[str, float] = {}
    after: str | None = None
    for _ in range(max_pages):
        params = {"instId": inst_id, "bar": "1Dutc", "limit": 100}
        if after:
            params["after"] = after
        resp = requests.get(
            f"{OKX_BASE}/api/v5/market/history-candles", params=params,
            headers=HEADERS, timeout=30,
        )
        j = resp.json()
        if j.get("code") != "0":
            print(f"  OKX 报错 code={j.get('code')} msg={j.get('msg')}")
            break
        rows = j.get("data") or []
        if not rows:
            break
        for r in rows:
            day = datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            out[day] = float(r[4])
        oldest = min(int(r[0]) for r in rows)
        if after is not None and oldest >= int(after):
            break
        after = str(oldest)
    return out


def sma(values: list[float], period: int) -> float | None:
    return sum(values[-period:]) / period if len(values) >= period else None


def weekly_from_daily(daily: dict[str, float]) -> list[tuple[str, float]]:
    """把日线聚合成自然周（ISO 周，周一起算），返回 [(周一日期, 该周最后收盘)]。"""
    weeks: dict[str, tuple[str, float]] = {}
    for day in sorted(daily):
        d = datetime.strptime(day, "%Y-%m-%d").date()
        monday = d.fromordinal(d.toordinal() - d.weekday()).strftime("%Y-%m-%d")
        prev = weeks.get(monday)
        if prev is None or day > prev[0]:
            weeks[monday] = (day, daily[day])
    return [(m, v) for m, (_, v) in sorted(weeks.items())]


def main() -> int:
    print("=" * 76)
    print("OKX TQQQ-USDT-SWAP  跟踪质量核查（日线按日期对齐）")
    print("=" * 76)

    us = fetch_yahoo_daily("2y")
    print(f"\n[美股] Yahoo TQQQ 日线 {len(us)} 根，{min(us)} ~ {max(us)}")
    print(f"       最新收盘 {us[max(us)]:.2f}")

    okx = fetch_okx_daily_utc("TQQQ-USDT-SWAP")
    print(f"\n[OKX ] 永续 1Dutc 日线 {len(okx)} 根，{min(okx)} ~ {max(okx)}")
    print(f"       最新收盘 {okx[max(okx)]:.2f}")

    common = sorted(set(us) & set(okx))
    print(f"\n[对齐] 共同交易日 {len(common)} 天：{common[0]} ~ {common[-1]}")
    if len(common) < 50:
        print("共同交易日不足，无法给出可靠结论")
        return 1

    # ---- 逐日价差 ----
    diffs = [(okx[d] - us[d]) / us[d] * 100 for d in common]
    us_rets, okx_rets = [], []
    for i in range(1, len(common)):
        p, c = common[i - 1], common[i]
        us_rets.append(us[c] / us[p] - 1)
        okx_rets.append(okx[c] / okx[p] - 1)

    corr = statistics.correlation(us_rets, okx_rets)
    same = sum(1 for a, b in zip(us_rets, okx_rets) if (a > 0) == (b > 0))
    gaps = [abs(a - b) for a, b in zip(us_rets, okx_rets)]

    print("\n" + "-" * 76)
    print("日线级别")
    print("-" * 76)
    print(f"  价差         : 平均 {statistics.mean(diffs):+.3f}%   "
          f"最大 {max(abs(d) for d in diffs):.3f}%   "
          f"标准差 {statistics.pstdev(diffs):.3f}%")
    print(f"  日收益相关   : {corr:.4f}")
    print(f"  同向日占比   : {same}/{len(us_rets)} = {same/len(us_rets)*100:.1f}%")
    print(f"  日收益偏差   : 平均 {statistics.mean(gaps)*100:.4f}pp   "
          f"最大 {max(gaps)*100:.3f}pp")

    # ---- 周线：两套口径 ----
    print("\n" + "-" * 76)
    print("周线级别（关键：趋势轨用的就是周线）")
    print("-" * 76)
    us_weeks = weekly_from_daily(us)
    okx_weeks = weekly_from_daily(okx)
    print(f"  美股周线（周一起算，含完整 2 年）: {len(us_weeks)} 周")

    us_map = dict(us_weeks)
    okx_map = dict(okx_weeks)
    cw = sorted(set(us_map) & set(okx_map))
    print(f"  两套周线共同周: {len(cw)} 周")

    # OKX 自己的 1W 周线（口径不同）单独对比
    resp = requests.get(
        f"{OKX_BASE}/api/v5/market/history-candles",
        params={"instId": "TQQQ-USDT-SWAP", "bar": "1W", "limit": 100},
        headers=HEADERS, timeout=30,
    )
    print(f"  OKX 原生 1W 周线: {len(resp.json().get('data') or [])} 根（这就是现在的 15 根限制）")

    # ---- 用长历史美股数据算 MA20/MA30 ----
    print("\n" + "-" * 76)
    print("用美股本土长历史能算出什么（这是补缺口的核心价值）")
    print("-" * 76)
    closes = [v for _, v in us_weeks]
    for period in (10, 20, 30):
        ma = sma(closes, period)
        prev_ma = sma(closes[:-1], period)
        if ma is None:
            print(f"  MA{period}: 数据不足")
            continue
        trend = "上行" if ma > prev_ma else "下行"
        print(f"  MA{period} = {ma:.2f} ({trend})   现价 {closes[-1]:.2f}   "
              f"偏离 {(closes[-1]-ma)/ma*100:+.2f}%")

    # ---- 用重叠周比较 MA10 状态 ----
    print("\n" + "-" * 76)
    print("重叠周内，两套数据算出的趋势状态是否一致")
    print("-" * 76)
    for period in (5, 10):
        agree = total = 0
        for i in range(period + 1, len(cw) + 1):
            w = cw[:i]
            uc = [us_map[x] for x in w]
            oc = [okx_map[x] for x in w]
            mu, mp = sma(uc, period), sma(uc[:-1], period)
            ou, op = sma(oc, period), sma(oc[:-1], period)
            if None in (mu, mp, ou, op):
                continue
            su = "up" if uc[-1] > mu and mu > mp else ("down" if uc[-1] < mu and mu < mp else "flat")
            so = "up" if oc[-1] > ou and ou > op else ("down" if oc[-1] < ou and ou < op else "flat")
            total += 1
            if su == so:
                agree += 1
        if total:
            print(f"  MA{period} 状态一致: {agree}/{total} = {agree/total*100:.1f}%")

    print("\n" + "=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
