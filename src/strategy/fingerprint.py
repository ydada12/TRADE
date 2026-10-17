"""六种指纹模板 —— 去重的键。

每条信号推送前都会先算一个指纹（fingerprint），闸口拿它做三件事：
冷却判定、失败重试计数、落历史。指纹格式一旦改动，等于把同一批信号当成
新信号重发一遍，所以这里**逐字复刻**原 run.py 里的格式，不许顺手改。

模板：
  trend   |inst_id|state                        趋势轨（状态切换）
  event   |event_fingerprint                    事件轨：公布前剧本
  event   |event_fingerprint|pre                事件轨：公布前剧本（推送用）
  event   |event_fingerprint|post|inst_id       事件轨：公布后结果
  world   |url_key|inst_id                      国际大事轨
  session |inst_id|window_label|b{bucket}       重点时段盘中波动
  short   |inst_id|{level:.4g}                  做空观察位
  short   |inst_id|{level:.4g}|fakebreak        做空观察位：假突破回落
  retry   |<上面任意一条指纹>                    失败重试计数键
"""

from __future__ import annotations


def trend_fp(inst_id: str, state: str) -> str:
    return f"trend|{inst_id}|{state}"


def event_fp(event_fingerprint: str) -> str:
    return f"event|{event_fingerprint}"


def event_pre_fp(base_fp: str) -> str:
    """在事件基础指纹（event|<fp>）后追加 |pre，得到公布前剧本的推送指纹。"""
    return f"{base_fp}|pre"


def event_post_fp(base_fp: str, inst_id: str) -> str:
    """在事件基础指纹（event|<fp>）后追加 |post|<inst>，得到公布后结果的推送指纹。"""
    return f"{base_fp}|post|{inst_id}"


def world_fp(url_key: str, inst_id: str) -> str:
    return f"world|{url_key}|{inst_id}"


def session_fp(inst_id: str, window_label: str, bucket: int) -> str:
    return f"session|{inst_id}|{window_label}|b{bucket}"


def short_fp(inst_id: str, level: float) -> str:
    return f"short|{inst_id}|{level:.4g}"


def short_fake_break_fp(inst_id: str, resistance: float) -> str:
    return f"short|{inst_id}|{resistance:.4g}|fakebreak"


def retry_fp(fingerprint: str) -> str:
    return f"retry|{fingerprint}"
