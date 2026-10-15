"""把历史日志里「已推送」的告警回填成结构化记录（面板标注 K 线用）。

背景：`state/alerts.jsonl` 是从「加了这个模块之后」才开始记的，之前推过的告警
只散落在 `logs/monitor-*.log` 里，面板上看不到历史位置。这个脚本负责把它们捞回来。

安全用法（默认就是 dry-run，绝不默认写盘）：
  python tools/backfill_alerts.py                 # 只解析、只打印统计，不写
  python tools/backfill_alerts.py --write         # 确认无误后再落盘
  python tools/backfill_alerts.py --since 2026-10-12 --write

原则：**宁可少回填，也不写错一条**。
  - 标题/时间/价位任何一项解析不出来，这条就跳过（只统计「未匹配」条数）
  - 回填记录一律打 `source=log_backfill` 标记，面板上与实时记录区分显示
  - 与已有历史按 (品种, 类型, 数据时间, 现价) 去重，重复跑不会重复写

红线：只读本地日志、只写本地 JSONL，不联网、不发推送、不做任何交易动作。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alert import short_symbol  # noqa: E402
from src.config import load_config  # noqa: E402
from src.dashboard.history import (  # noqa: E402
    SOURCE_BACKFILL,
    AlertHistory,
    AlertRecord,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

UTC8 = timezone(timedelta(hours=8))

# 一行日志的开头：2026-10-15 00:03:35 [INFO] src.notify.serverchan: ...
_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (\S+?):\s?(.*)$")
_PUSH_OK = re.compile(r"推送成功（第 \d+ 次尝试）：(.+?)\s+\|\s+HTTP")
_BODY_START = re.compile(r"^推送正文：\s?(.*)$")
_HEADER = re.compile(r"^##\s*【(.+?)】(.+?)\s*$")
# ---- 旧版正文（Markdown 排版之前）的字段格式，历史日志里大量存在 ----
_LEGACY_KIND = re.compile(r"^【信号类型】(.+?)\s*$")
_LEGACY_SYMBOL = re.compile(r"^【品种】(.+?)\s*$")
_LEGACY_INST = re.compile(r"（([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)）\s*$")
_LEGACY_PRICE = re.compile(r"【(?:当前价|当前位置)】\s*([\d,.]+)")
_LEGACY_LEVELS = re.compile(r"【关键位】\s*支撑\s*([\d,.]+)\s*　?\s*压力\s*([\d,.]+)")
# 早期的趋势轨是把支撑/压力分成两行写的（【支撑位】X / 【压力位】Y）
_LEGACY_LEVEL_ONE = re.compile(r"^【(支撑位|压力位)】\s*([\d,.]+|数据源未提供)")
_LEGACY_TRIGGER = re.compile(r"^【触发条件】(.+?)\s*$")
# 正文里的数据时间：优先「北京时间」，其次「数据时间 ... UTC」
_BJ_TIME = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*北京时间")
_UTC_TIME = re.compile(r"数据时间\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*UTC")
_NUM = re.compile(r"\*\*(现价|压力位|支撑位)\*\*\s*([^\s　｜（(]+)")
_SKIP_KINDS = {"测试"}
# 旧版正文的类型名 → 新版正文的类型名（同一类信号只留一种徽章）
_LEGACY_KIND_MAP = {
    "事件（公布前）": "事件预警",
    "事件（公布后）": "事件结果",
    "趋势": "趋势",
}


def _to_float(text: str) -> float | None:
    if not text or text in {"数据源未提供", "未知", "-", "--"}:
        return None
    cleaned = text.replace(",", "").replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_log(lines: list[str], log_name: str) -> tuple[list[dict], list[str], int]:
    """解析一个日志文件，返回 (记录字典列表, 未匹配原因列表, 主动跳过条数)。

    只认「推送成功」那一行紧跟着的同一个「推送正文：」块，避免把半截日志当告警。
    测试推送（--test-push）不算业务告警，主动跳过、不计入未匹配。
    """
    records: list[dict] = []
    unmatched: list[str] = []
    skipped = 0
    index = 0
    total = len(lines)
    while index < total:
        hit = _LOG_LINE.match(lines[index].rstrip("\n"))
        if not hit:
            index += 1
            continue
        stamp, _level, logger, message = hit.groups()
        if logger != "src.notify.serverchan" or "推送成功" not in message:
            index += 1
            continue

        title_hit = _PUSH_OK.search(message)
        index += 1
        # 收正文：从下一行开始，直到遇到下一条日志行
        body: list[str] = []
        if index < total:
            body_hit = _LOG_LINE.match(lines[index].rstrip("\n"))
            if body_hit and body_hit.group(3) == "src.notify.serverchan":
                start = _BODY_START.match(body_hit.group(4))
                if start:
                    rest = start.group(1)
                    if rest.strip():
                        body.append(rest)
                    index += 1
                    while index < total and not _LOG_LINE.match(lines[index].rstrip("\n")):
                        body.append(lines[index].rstrip("\n"))
                        index += 1

        if not title_hit:
            # 「按目标逐个报成功」与「多目标汇总」这两种行没有标题，属测试/链路日志
            skipped += 1
            continue
        if not body:
            unmatched.append(f"{log_name} {stamp} 有推送成功行但找不到完整正文")
            continue

        record = _parse_body(title_hit.group(1).strip(), body, stamp)
        if record is None:
            unmatched.append(f"{log_name} {stamp} 正文解析不完整（缺标题/时间）")
        elif record.get("skipped"):
            skipped += 1
        else:
            records.append(record)
    return records, unmatched, skipped


def _parse_body(title: str, body: list[str], stamp: str) -> dict | None:
    """从一条推送正文里还原结构化字段；缺关键信息返回 None。"""
    text = "\n".join(body)
    header = None
    for line in body:
        matched = _HEADER.match(line.strip())
        if matched:
            header = matched
            break
    if header is None:
        return _parse_legacy_body(title, body, stamp)
    short_name, kind = header.group(1).strip(), header.group(2).strip()
    if kind in _SKIP_KINDS:
        return {"skipped": True}

    # 数据时间：优先正文里的北京时间（= 当时取到的行情时间），退回推送时刻
    ts_ms = _time_to_ms(_BJ_TIME.search(text), UTC8)
    if ts_ms is None:
        ts_ms = _time_to_ms(_UTC_TIME.search(text), timezone.utc)
    if ts_ms is None:
        ts_ms = _stamp_to_ms(stamp)
    if ts_ms is None:
        return None

    numbers = {name: _to_float(value) for name, value in _NUM.findall(text)}
    happened = ""
    for line in body:
        stripped = line.strip()
        if stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4:
            happened = stripped.strip("*")
            break

    return {
        "title": title,
        "short_name": short_name,
        "signal_type": kind,
        "ts_ms": ts_ms,
        "pushed_at": stamp,
        "price": numbers.get("现价"),
        "support": numbers.get("支撑位"),
        "resistance": numbers.get("压力位"),
        "what_happened": happened,
    }


def _parse_legacy_body(title: str, body: list[str], stamp: str) -> dict | None:
    """旧版正文（Markdown 排版之前的【字段】= 值 形式）。

    字段是显式写出来的（【品种】里直接带 inst_id），所以这一支解析反而更稳。
    消息类型做归一化，否则同一个「事件」在面板上会出现两种徽章。
    """
    text = "\n".join(body)
    kind = symbol_line = trigger = ""
    for line in body:
        stripped = line.strip()
        if not kind:
            matched = _LEGACY_KIND.match(stripped)
            if matched:
                kind = matched.group(1).strip()
                continue
        if not symbol_line:
            matched = _LEGACY_SYMBOL.match(stripped)
            if matched:
                symbol_line = matched.group(1).strip()
                continue
        if not trigger:
            matched = _LEGACY_TRIGGER.match(stripped)
            if matched:
                trigger = matched.group(1).strip()
    kind = _LEGACY_KIND_MAP.get(kind, kind)
    if not kind:
        return None
    if kind in _SKIP_KINDS:
        return {"skipped": True}

    inst_id = ""
    short_name = ""
    matched = _LEGACY_INST.search(symbol_line)
    if matched:
        inst_id = matched.group(1)
        short_name = short_symbol(inst_id, symbol_line)
    elif symbol_line:
        short_name = symbol_line.split()[0][:10]

    ts_ms = _time_to_ms(_BJ_TIME.search(text), UTC8)
    if ts_ms is None:
        ts_ms = _time_to_ms(_UTC_TIME.search(text), timezone.utc)
    if ts_ms is None:
        ts_ms = _stamp_to_ms(stamp)
    if ts_ms is None:
        return None

    price = _LEGACY_PRICE.search(text)
    levels = _LEGACY_LEVELS.search(text)
    support = _to_float(levels.group(1)) if levels else None
    resistance = _to_float(levels.group(2)) if levels else None
    for line in body:
        matched = _LEGACY_LEVEL_ONE.match(line.strip())
        if not matched:
            continue
        if matched.group(1) == "支撑位" and support is None:
            support = _to_float(matched.group(2))
        elif matched.group(1) == "压力位" and resistance is None:
            resistance = _to_float(matched.group(2))
    return {
        "title": title,
        "short_name": short_name,
        "signal_type": kind,
        "ts_ms": ts_ms,
        "pushed_at": stamp,
        "price": _to_float(price.group(1)) if price else None,
        "support": support,
        "resistance": resistance,
        "what_happened": trigger,
    }


def _time_to_ms(match, tz) -> int | None:
    if not match:
        return None
    try:
        dt = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _stamp_to_ms(stamp: str) -> int | None:
    """日志行自己的时间戳（本机时钟就是北京时间，与正文里的北京时间一致）。"""
    try:
        dt = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC8)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def resolve_inst_id(short_name: str, symbols: list[dict]) -> str:
    """靠标题短名反查 instance id；查不到就给空（面板按「未归属品种」显示）。"""
    for symbol in symbols:
        inst_id = str(symbol.get("inst_id") or "")
        if not inst_id:
            continue
        if short_symbol(inst_id, str(symbol.get("name") or "")) == short_name:
            return inst_id
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="把历史日志里的已推送告警回填成 alerts.jsonl")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--log-dir", default=None, help="日志目录（默认取 app.log_dir）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只解析不落盘（本来就是默认行为，这个开关只是显式说明）")
    parser.add_argument("--write", action="store_true", help="真的落盘（不加此参数只解析）")
    parser.add_argument("--since", default=None, help="只回填该日期（YYYY-MM-DD）及之后的日志")
    args = parser.parse_args()

    cfg = load_config(args.config)
    app = cfg.get("app", {}) or {}
    log_dir = Path(args.log_dir) if args.log_dir else cfg.resolve_path(str(app.get("log_dir", "logs")))
    out_path = cfg.resolve_path(str(app.get("alert_history_file", "state/alerts.jsonl")))
    symbols = cfg.symbols

    if not log_dir.is_dir():
        print(f"[fail] 找不到日志目录：{log_dir}")
        return 1

    files = sorted(log_dir.glob("monitor-*.log"))
    if args.since:
        files = [f for f in files if f.stem.replace("monitor-", "") >= args.since]
    if not files:
        print(f"[info] {log_dir} 下没有可回填的 monitor-*.log")
        return 0

    print(f"日志目录：{log_dir}")
    print(f"目标文件：{out_path}")
    print(f"待解析日志：{len(files)} 个\n")

    parsed: list[dict] = []
    unmatched: list[str] = []
    skipped = 0
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            unmatched.append(f"{path.name} 读取失败：{exc}")
            continue
        records, misses, skips = parse_log(lines, path.name)
        parsed.extend(records)
        unmatched.extend(misses)
        skipped += skips

    history = AlertHistory(out_path)
    existing = {(r.inst_id, r.signal_type, r.ts_ms, r.price) for r in history.load()}

    fresh: list[AlertRecord] = []
    duplicates = 0
    for item in parsed:
        inst_id = resolve_inst_id(item["short_name"], symbols)
        record = AlertRecord(
            ts_ms=item["ts_ms"],
            recorded_at=item["pushed_at"],
            inst_id=inst_id,
            symbol=inst_id or item["short_name"],
            short_name=item["short_name"],
            signal_type=item["signal_type"],
            title=item["title"],
            what_happened=item["what_happened"],
            price=item["price"],
            support=item["support"],
            resistance=item["resistance"],
            fingerprint=f"{item['signal_type']}|{inst_id}|{item['pushed_at']}",
            pushed_at=item["pushed_at"],
            source=SOURCE_BACKFILL,
        )
        if (record.inst_id, record.signal_type, record.ts_ms, record.price) in existing:
            duplicates += 1
            continue
        existing.add((record.inst_id, record.signal_type, record.ts_ms, record.price))
        fresh.append(record)

    by_kind: dict[str, int] = {}
    by_symbol: dict[str, int] = {}
    for record in fresh:
        by_kind[record.signal_type] = by_kind.get(record.signal_type, 0) + 1
        key = record.short_name or "(未识别)"
        by_symbol[key] = by_symbol.get(key, 0) + 1

    print(f"解析到推送记录：{len(parsed)} 条")
    print(f"跳过（测试推送 / 多目标汇总行）：{skipped} 条")
    print(f"其中已在历史里（跳过）：{duplicates} 条")
    print(f"可回填：{len(fresh)} 条")
    if by_kind:
        print("  按类型：" + "，".join(f"{k} {v}" for k, v in sorted(by_kind.items(), key=lambda x: -x[1])))
    if by_symbol:
        print("  按品种：" + "，".join(f"{k} {v}" for k, v in sorted(by_symbol.items(), key=lambda x: -x[1])))
    if unmatched:
        print(f"\n未匹配：{len(unmatched)} 条（宁可不回填也不写错）")
        for line in unmatched[:15]:
            print(f"  - {line}")
        if len(unmatched) > 15:
            print(f"  ...（其余 {len(unmatched) - 15} 条省略）")

    if not args.write:
        print("\n[dry-run] 没有写入任何内容。确认上面统计无误后，加 --write 再跑一次。")
        return 0

    written = sum(1 for record in sorted(fresh, key=lambda r: r.ts_ms or 0) if history.append(record))
    print(f"\n[write] 已写入 {written} 条 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
