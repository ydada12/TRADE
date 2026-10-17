"""推送措辞常量：极短状态名 + 重点提醒前缀。

这里是「微信通知栏上会显示什么字」的集中住所。改措辞先看这个文件，
改阈值去看 config.yaml。
"""

from __future__ import annotations

# 标题用的极短状态名（微信通知栏只显示标题）
STATE_SHORT = {"bull": "多头", "bear": "空头", "range": "震荡", "unknown": "数据不足"}

# 被判为「重点提醒」的信号，标题统一加此前缀（醒目、且免打扰期间照常推送）
PRIORITY_PREFIX = "【重点】"
