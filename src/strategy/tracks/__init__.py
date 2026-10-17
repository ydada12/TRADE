"""五条轨道的规则与文案。

趋势轨(trend) / 事件轨(event) / 国际大事(world) / 重点时段盘中(session) /
做空观察位(short) 各占一个文件，以 mixin 形式挂在 Monitor 上（零调用点改动）。

改动指引：
  - 触发条件 / 打分阈值 -> config.yaml（trend / event / world_events /
    session_watch / short_watch 各段）
  - 指纹（去重键）-> src/strategy/fingerprint.py
  - 极短状态名与重点前缀 -> src/strategy/wording.py
  - 跨轨方向提示 -> src/strategy/memo.py
  - 出不出得去（冷却/免打扰/重试）-> src/strategy/gate.py
"""
