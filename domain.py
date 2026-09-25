"""领域通用件：异常、时间戳、规范化 JSON。

供订阅资料、影响判断、名单保存等模块与 HTTP 入口共用，避免相互循环导入。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
