"""日志：同时写文件（方便回头查）和标准输出。

文件按天切分：logs/monitor-YYYY-MM-DD.log
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_CONFIGURED = False
LOG_DIR: Path | None = None
_CURRENT_DATE: str | None = None
_CURRENT_FILE: Path | None = None


def setup_logging(log_dir: Path, level: str = "INFO", tag: str = "monitor",
                  force: bool = False) -> Path:
    """初始化日志，返回日志文件路径。

    跨天会自动换文件：主循环每轮都会调用一次本函数，
    日期没变时它立刻返回（几乎零开销），日期变了就切到新文件。
    否则常驻进程会一直往启动那天命名的文件里写，你要回头查「今天」的日志会找不到。
    """
    global _CONFIGURED, LOG_DIR, _CURRENT_DATE, _CURRENT_FILE

    log_dir.mkdir(parents=True, exist_ok=True)
    LOG_DIR = log_dir

    today = datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"{tag}-{today}.log"

    if _CONFIGURED and not force and _CURRENT_DATE == today:
        return _CURRENT_FILE or log_file

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # 关掉旧 handler（换天时要把旧文件句柄释放掉）
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    for noisy in ("urllib3", "requests", "akshare", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    _CURRENT_DATE = today
    _CURRENT_FILE = log_file
    if force or _CURRENT_FILE is None:
        logging.getLogger(__name__).info("日志已切换到 %s", log_file)
    return log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
