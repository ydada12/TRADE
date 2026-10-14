"""配置加载：config.yaml + 环境变量（key 一律不落盘）。

设计原则（对应红线 4）：
  - 所有可变参数集中在 config.yaml
  - API key 只能来自环境变量（或本地 .env，.env 不写进版本库）
  - 代码里绝不出现任何明文 key
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """配置有问题时抛出，宁可明确报错也不猜。"""


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析器。

    只做一件事：把 .env 里的键值对塞进 os.environ（已存在的环境变量优先，
    不覆盖）。这样「系统环境变量」永远比「文件」优先级高。
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class Config:
    """一个薄封装，提供点号取值与必填校验。"""

    def __init__(self, data: dict[str, Any], path: Path) -> None:
        self._data = data
        self.path = path

    # ---- 基础访问 ----
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel or value is None:
            raise ConfigError(f"config.yaml 缺少必填项：{dotted}")
        return value

    @property
    def symbols(self) -> list[dict[str, Any]]:
        raw = self.get("symbols") or []
        if not isinstance(raw, list):
            raise ConfigError("config.yaml 的 symbols 必须是列表")
        return [s for s in raw if isinstance(s, dict)]

    def enabled_symbols(self) -> list[dict[str, Any]]:
        return [s for s in self.symbols if s.get("enabled", True)]

    def symbol_by_inst_id(self, inst_id: str) -> dict[str, Any] | None:
        for s in self.symbols:
            if s.get("inst_id") == inst_id:
                return s
        return None

    # ---- 路径 ----
    def resolve_path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    # ---- key 读取（永不落盘）----
    def read_secret(self, env_name: str) -> str | None:
        value = os.environ.get(env_name)
        if value is None:
            return None
        value = value.strip()
        if not value or value.lower().startswith(("sk-在这里", "sct在这里")):
            return None
        return value


def load_config(config_path: str | Path | None = None) -> Config:
    """加载配置。默认顺序：显式路径 -> 项目根 config.yaml -> config.example.yaml。"""
    if config_path is None:
        candidate = PROJECT_ROOT / "config.yaml"
        if not candidate.is_file():
            candidate = PROJECT_ROOT / "config.example.yaml"
        config_path = candidate
    path = Path(config_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file():
        raise ConfigError(f"找不到配置文件：{path}")

    _load_dotenv(PROJECT_ROOT / ".env")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover
        raise ConfigError(f"config 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config.yaml 顶层必须是字典")

    cfg = Config(data, path)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if not cfg.symbols:
        raise ConfigError("config.yaml 至少要配置一个 symbols")
    seen: set[str] = set()
    for sym in cfg.symbols:
        inst_id = sym.get("inst_id")
        if not inst_id:
            raise ConfigError(f"symbols 里有一条缺少 inst_id：{sym}")
        if inst_id in seen:
            raise ConfigError(f"symbols 里 inst_id 重复：{inst_id}")
        seen.add(inst_id)
        for period in sym.get("ma_periods") or []:
            if not isinstance(period, int) or period < 2:
                raise ConfigError(f"{inst_id} 的 ma_periods 必须是不小于 2 的整数")
        pos = sym.get("position") or {}
        if pos.get("entry_price") is not None and pos.get("side") not in {"long", "short"}:
            raise ConfigError(
                f"{inst_id} 填了开仓价 {pos.get('entry_price')}，"
                f"但 position.side 不是 long/short，请补齐方向"
            )
