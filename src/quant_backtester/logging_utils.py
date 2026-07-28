from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import Any

from quant_backtester.logging_context import (
    current_trace_ids,
    job_id_var,
    request_id_var,
    run_id_var,
    tenant_id_var,
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            payload.update(event)
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
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level.upper())
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.handlers = [handler]
