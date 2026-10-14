"""运行状态检查（给用户看的"它在不在跑"）。

设计要点：
  - 最可靠的信号是**日志心跳**，而不是"进程还在不在"。
    进程可能卡死（网络阻塞），但心跳停了就是真的没在干活。
  - 全部输出中文，由 Python 打印，绕开 PowerShell 5.1 的编码坑。
  - 不依赖第三方库，任何环境都能跑。

用法：
    python tools/alive.py
    （或双击项目根目录的 check.bat）
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UTC8 = timezone(timedelta(hours=8))
LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

OK = "✓"
BAD = "✗"
WARN = "⚠"


def newest(pattern: str, folder: Path) -> Path | None:
    if not folder.is_dir():
        return None
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def last_timestamp(log_path: Path | None) -> datetime | None:
    """从日志尾部往回找第一条带时间戳的行。"""
    if not log_path or not log_path.is_file():
        return None
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-400:]):
        match = LOG_TS.match(line)
        if match:
            try:
                return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC8)
            except ValueError:
                continue
    return None


def read_config() -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    path = ROOT / "config.yaml"
    if not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def find_project_processes() -> dict[str, list[int]]:
    """只找【属于本项目】的进程。

    关键：不能只看进程名。机器上可能有别的 python.exe（比如其它工具），
    误报会让用户以为监控重复启动了。用命令行里的项目路径来判定。
    """
    result: dict[str, list[int]] = {"monitor": [], "supervisor": []}
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='powershell.exe'\" | "
        "ForEach-Object { \"{0}|{1}|{2}\" -f $_.ProcessId, $_.Name, $_.CommandLine }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=30,
        )
        text = out.stdout.decode("utf-8", errors="replace")
        if "Win32_Process" in text and "|" not in text:
            text = out.stdout.decode("mbcs", errors="replace")
    except Exception:
        return result

    marker = str(ROOT).lower()
    for line in text.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        pid_text, name, cmd = parts[0].strip(), parts[1].strip().lower(), parts[2].lower()
        if marker not in cmd:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if name.startswith("python") and "run.py" in cmd:
            result["monitor"].append(pid)
        elif name.startswith("powershell") and "run_forever" in cmd:
            result["supervisor"].append(pid)
    return result


def task_state() -> str:
    try:
        out = subprocess.run(
            ["schtasks", "/query", "/tn", "DualTrackSignalMonitor", "/fo", "LIST"],
            capture_output=True, timeout=15,
        )
        text = out.stdout.decode("mbcs", errors="replace")
    except Exception:
        return "查询失败"
    for line in text.splitlines():
        if "状态" in line or "Status" in line:
            return line.split(":", 1)[-1].strip()
    return "未知"


def main() -> int:
    now = datetime.now(UTC8)
    cfg = read_config()
    base_interval = float((cfg.get("app") or {}).get("poll_interval_seconds", 300))
    session_interval = float((cfg.get("session_watch") or {}).get("poll_interval_seconds", 60))

    log_dir = ROOT / "logs"
    monitor_log = newest("monitor-*.log", log_dir)
    supervisor_log = newest("supervisor-*.log", log_dir)
    last_beat = last_timestamp(monitor_log)
    last_sup = last_timestamp(supervisor_log)

    stop_flag = ROOT / "STOP_MONITOR.flag"
    procs = find_project_processes()
    monitors = procs["monitor"]
    supervisors = procs["supervisor"]
    state = task_state()

    age_min = (now - last_beat).total_seconds() / 60.0 if last_beat else None

    # ---------- 结论 ----------
    if stop_flag.exists():
        verdict, mark = "已手动停止（存在 STOP_MONITOR.flag）", BAD
    elif len(monitors) > 1:
        verdict, mark = (f"发现 {len(monitors)} 个监控进程在同时跑 —— "
                         f"会导致重复推送，请立刻处理"), BAD
    elif len(supervisors) > 1:
        verdict, mark = (f"发现 {len(supervisors)} 个守护进程 —— "
                         f"它们会各自拉起一个监控，请立刻处理"), BAD
    elif not monitors:
        verdict, mark = "没有监控进程在跑", BAD
    elif age_min is None:
        verdict, mark = "找不到日志，无法判断（可能从未成功启动过）", BAD
    elif age_min <= max(3.0, base_interval / 60.0 * 1.5):
        verdict, mark = "监控正在正常运行", OK
    elif age_min <= 15:
        verdict, mark = "监控在跑，但心跳偏慢，可能刚才卡了一下", WARN
    else:
        verdict, mark = f"心跳已停 {age_min:.0f} 分钟，监控很可能已卡死", BAD

    print("=" * 66)
    print(f" 双轨信号监控 —— 运行状态检查    {now:%Y-%m-%d %H:%M:%S} 北京时间")
    print("=" * 66)
    print()
    print(f"【结论】{mark} {verdict}")
    if last_beat and monitors:
        print(f"        最后一次心跳：{last_beat:%Y-%m-%d %H:%M:%S}"
              f"（{age_min:.1f} 分钟前）")
    print()

    print("--- 进程与计划任务 ---")
    print(f"计划任务 DualTrackSignalMonitor : {state}")
    if monitors:
        for pid in monitors:
            print(f"监控进程 (run.py --loop)       : pid {pid}")
    else:
        print(f"监控进程 (run.py --loop)       : {BAD} 没找到")
    if supervisors:
        for pid in supervisors:
            print(f"守护进程 (run_forever.ps1)     : pid {pid}")
    else:
        print(f"守护进程 (run_forever.ps1)     : {BAD} 没找到")
    if len(monitors) > 1:
        print(f"  {BAD} 重复启动！多余的进程会让你收到重复推送。")
        print("     处理：先跑 scripts\\unregister_autostart.ps1，再重新 register。")
    if len(supervisors) > 1:
        print(f"  {BAD} 守护进程重复！每个都会拉起一个监控。")
        print("     处理：先跑 scripts\\unregister_autostart.ps1，再重新 register。")

    print()
    print("--- 心跳 ---")
    if monitor_log:
        print(f"最新主日志  : logs/{monitor_log.name}")
    print(f"最近一轮日志: {last_beat:%Y-%m-%d %H:%M:%S}" if last_beat else "最近一轮日志: 无")
    if last_sup:
        print(f"守护日志    : {last_sup:%Y-%m-%d %H:%M:%S}")
    print(f"轮询节奏    : 平时 {base_interval:.0f} 秒 / 重点时段 {session_interval:.0f} 秒")

    print()
    print("--- 最近日志尾部 ---")
    if monitor_log:
        try:
            tail = monitor_log.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]
            for line in tail:
                print("  " + line[:150])
        except OSError:
            print("  读取失败")
    else:
        print("  没有日志文件")

    print()
    print("--- 当前趋势状态 ---")
    state_file = ROOT / "state" / "state.json"
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        trend = data.get("trend_state") or {}
        if trend:
            for inst_id, info in trend.items():
                print(f"  {inst_id:<20} {info.get('state', '?'):<8}"
                      f"（周 {info.get('as_of_week', '?')}，更新于 {info.get('updated_at', '?')}）")
        else:
            print("  （还没有记录）")
        print(f"  已推送过的信号指纹：{len(data.get('sent') or {})} 条")
        print(f"  已跟踪事件          ：{len(data.get('events') or {})} 条")
        print(f"  盘中监控记忆        ：{len(data.get('session') or {})} 条")
    else:
        print("  没有 state.json（还没跑过）")

    print()
    print("=" * 66)
    print(" 提示：本工具查的是【监控程序】。")
    print("       AI 助手（我）不是常驻服务，只在你发消息时才工作。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
