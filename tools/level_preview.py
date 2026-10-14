"""预览各品种的关键位推导结果（真实数据，只读）。

运行：python tools/level_preview.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.datasource.okx import OkxClient
from src.datasource.us_stock import UsStockClient
from src.modules.levels import derive_levels


def main() -> int:
    cfg = load_config()
    us = UsStockClient()
    okx = OkxClient()

    for symbol in cfg.enabled_symbols():
        inst_id = symbol["inst_id"]
        ticker = okx.get_ticker(inst_id)
        source = str(symbol.get("history_source") or "okx")
        if source == "us_stock":
            candles = us.get_weekly(str(symbol["us_symbol"]),
                                    str(symbol.get("us_history_range", "5y")))
            src_desc = f"美股本土 {symbol['us_symbol']}"
        else:
            candles = okx.get_weekly_candles(inst_id, "1W", 300)
            src_desc = "OKX 周K"
        confirmed = [c for c in candles if c.confirmed]

        lc = symbol.get("levels") or {}
        levels = derive_levels(
            inst_id=inst_id,
            candles=confirmed,
            current_price=ticker.last,
            lookback_weeks=int(lc.get("lookback_weeks", 12)),
            manual_support=lc.get("manual_support"),
            manual_resistance=lc.get("manual_resistance"),
            min_distance_pct=float(lc.get("min_distance_pct", 1.5)),
        )

        sup, res = levels.nearest_support(), levels.nearest_resistance()
        print(f"\n{inst_id}   （{src_desc}）")
        print(f"  现价 {ticker.last:g}   已收盘周K {len(confirmed)} 根")
        if sup:
            print(f"  最近支撑 {sup:g}   （距现价 {(ticker.last - sup) / ticker.last * 100:+.2f}%）")
        if res:
            print(f"  最近压力 {res:g}   （距现价 {(res - ticker.last) / ticker.last * 100:+.2f}%）")
        print(f"  支撑候选 {[round(v, 2) for v in levels.supports[:6]]}")
        print(f"  压力候选 {[round(v, 2) for v in levels.resistances[:6]]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
