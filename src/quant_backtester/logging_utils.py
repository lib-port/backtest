from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
from typing import Any

from quant_backtester.logging_context import (
    current_trace_ids,
    job_id_var,
    request_id_var,
    run_id_var,
    tenant_id_var,
)

SENSITIVE_KEY = re.compile(
    r"(authorization|cookie|credential|password|secret|token|api[_-]?key)",
    re.IGNORECASE,
)
URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@", re.I)
AUTHORIZATION_VALUE = re.compile(r"(?i)(authorization\s*[=:]\s*)(?:bearer\s+)?[^\s,;]+")
BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+={0,2}")
INLINE_SECRET = re.compile(
    r"(?i)(cookie|credential|password|secret|token|api[_-]?key)(\s*[=:]\s*)([^\s,;]+)"
)
RESERVED_LOG_FIELDS = frozenset(
    {
        "ts",
        "level",
        "logger",
        "msg",
        "exception",
        "request_id",
        "tenant_id",
        "job_id",
        "run_id",
        "trace_id",
        "span_id",
    }
)


def _redact_text(value: str) -> str:
    redacted = URL_CREDENTIALS.sub(r"\g<scheme>***:***@", value)
    redacted = AUTHORIZATION_VALUE.sub(r"\1[REDACTED]", redacted)
    redacted = BEARER_VALUE.sub("Bearer [REDACTED]", redacted)
    return INLINE_SECRET.sub(r"\1\2[REDACTED]", redacted)


def _redact_value(value: object, *, key: str | None = None) -> object:
    if key is not None and SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _redact_text(record.getMessage()),
        }
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            redacted_event = _redact_value(event)
            if isinstance(redacted_event, dict):
                payload.update(
                    {
                        key: value
                        for key, value in redacted_event.items()
                        if key not in RESERVED_LOG_FIELDS
                    }
                )
        for key, value in (
            ("request_id", request_id_var.get()),
            ("tenant_id", tenant_id_var.get()),
            ("job_id", job_id_var.get()),
            ("run_id", run_id_var.get()),
        ):
            if value is not None:
                payload[key] = value
        trace_id, span_id = current_trace_ids()
        if trace_id is not None:
            payload["trace_id"] = trace_id
            payload["span_id"] = span_id
        if record.exc_info:
            payload["exception"] = _redact_text(self.formatException(record.exc_info)[:16_384])
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    normalized_level = level.upper()
    if normalized_level not in logging.getLevelNamesMapping():
        raise ValueError(f"Unknown log level: {level}")
    root = logging.getLogger()
    root.setLevel(normalized_level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(normalized_level)
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.handlers = [handler]
