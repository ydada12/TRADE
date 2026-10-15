"""启动本地只读可视化面板。

用法：
  python tools/dashboard.py                 # 起服务并自动打开浏览器
  python tools/dashboard.py --no-browser    # 只起服务
  python tools/dashboard.py --port 8888 --refresh 30

它做四件事（全部只读，不接交易接口、不下单、不发推送）：
  1. 品种下拉：切换 config.yaml 里的任意品种（已停用的灰显但仍可查看）
  2. K 线：现成的行情图，支持 5m / 15m / 1H / 4H / 1D / 1W
  3. 历史消息：推送过的告警列表（来自 state/alerts.jsonl）
  4. 在 K 线上标注每条历史告警发生的时间与价位

安全问题：默认只监听 127.0.0.1（本机），不对外网暴露；端口被占用时直接报错，
绝不静默换端口——否则你以为在看 A 面板，实际看的是别的东西。
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.dashboard.server import DashboardServer  # noqa: E402
from src.dashboard.service import KLINE_MAX, DashboardService  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="本地只读可视化面板（K线 + 历史告警标注）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认只绑本机）")
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    parser.add_argument("--refresh", type=float, default=60.0,
                        help="行情缓存秒数，也是前端自动刷新的间隔（默认 60）")
    parser.add_argument("--kline-max", type=int, default=KLINE_MAX,
                        help=f"单次返回的最大 K 线根数（默认 {KLINE_MAX}）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if not WEB_ROOT.is_dir():
        print(f"[fail] 找不到前端目录：{WEB_ROOT}")
        return 1
    if not (WEB_ROOT / "index.html").is_file():
        print(f"[fail] 缺少前端文件：{WEB_ROOT / 'index.html'}")
        return 1

    cfg = load_config(args.config)
    service = DashboardService(cfg, ttl_seconds=args.refresh, kline_max=args.kline_max)

    if not _port_available(args.host, args.port):
        print(f"[fail] 端口 {args.host}:{args.port} 已被占用。"
              f"请先关掉占用它的程序，或换一个端口：--port 8766")
        return 1

    try:
        server = DashboardServer((args.host, args.port), service, WEB_ROOT)
    except OSError as exc:
        print(f"[fail] 无法在 {args.host}:{args.port} 上启动服务：{exc}")
        return 1

    url = f"http://{args.host}:{args.port}/"
    enabled = [s for s in service.symbols() if s["enabled"]]
    print("本地只读面板已启动（本页面只读展示，不构成任何买卖建议）")
    print(f"  地址：{url}")
    print(f"  品种：{len(service.symbols())} 个（启用 {len(enabled)} 个；已停用的灰显但仍可查看）")
    print(f"  缓存：{args.refresh:g} 秒；K线上限：{args.kline_max} 根")
    print(f"  告警历史：{service.history.path}")
    print("  按 Ctrl+C 停止")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
