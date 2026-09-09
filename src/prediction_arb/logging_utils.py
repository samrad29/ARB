from __future__ import annotations

import json
import logging
import sys
from typing import Any

_SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "access_key",
    "access_signature",
    "private_key",
    "secret",
    "password",
    "token",
}


class StructuredFormatter(logging.Formatter):
    def __init__(self, json_mode: bool = False) -> None:
        super().__init__()
        self.json_mode = json_mode

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "event_data", None)
        if isinstance(extra, dict):
            payload.update(_redact(extra))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if self.json_mode:
            return json.dumps(payload, default=str)
        extras = {k: v for k, v in payload.items() if k not in {"level", "logger", "message"}}
        suffix = f" {json.dumps(extras, default=str)}" if extras else ""
        return f"{payload['level']} {payload['logger']} {payload['message']}{suffix}"


def _redact(data: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in _SECRET_KEYS or any(part in key.lower() for part in ("secret", "credential")):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def setup_logging(level: str = "INFO", json_mode: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter(json_mode=json_mode))
    root = logging.getLogger("prediction_arb")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"event_data": fields})
