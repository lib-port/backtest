from __future__ import annotations

import json
import logging
import uuid

import pytest

from quant_backtester.logging_utils import JsonFormatter, configure_logging
from quant_backtester.observability import configure_telemetry
from quant_backtester.service.queue import SQSJobNotifier


def test_json_logging_redacts_nested_and_inline_secrets() -> None:
    record = logging.LogRecord(
        name="security-test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=12,
        msg=(
            "database=postgresql://operator:database-password@db/quant "
            "Authorization: Bearer header-token token=query-token"
        ),
        args=(),
        exc_info=None,
    )
    record.event = {
        "level": "FORGED",
        "msg": "forged",
        "request": {
            "password": "nested-password",
            "headers": {"Cookie": "session=secret", "Accept": "application/json"},
        },
    }

    document = json.loads(JsonFormatter().format(record))

    encoded = json.dumps(document)
    assert "database-password" not in encoded
    assert "header-token" not in encoded
    assert "query-token" not in encoded
    assert "nested-password" not in encoded
    assert "session=secret" not in encoded
    assert document["request"]["headers"]["Accept"] == "application/json"
    assert "postgresql://***:***@db/quant" in document["msg"]
    assert document["level"] == "ERROR"
    assert document["msg"] != "forged"


def test_logging_configuration_rejects_unknown_levels() -> None:
    with pytest.raises(ValueError, match="Unknown log level"):
        configure_logging("not-a-level")


def test_trace_sampling_ratio_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://telemetry.example.test")
    monkeypatch.setenv("QBT_TRACE_SAMPLE_RATIO", "1.1")
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        configure_telemetry(service_name="test")


def test_sqs_wakeup_validation_and_bounded_receive(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = str(uuid.uuid4())
    calls: dict[str, object] = {}

    class FakeSQSClient:
        def receive_message(self, **kwargs: object) -> dict[str, object]:
            calls.update(kwargs)
            return {
                "Messages": [
                    {
                        "ReceiptHandle": "receipt",
                        "Body": json.dumps(
                            {
                                "schema_version": 1,
                                "job_id": job_id,
                                "tenant_id": "tenant-a",
                            }
                        ),
                    }
                ]
            }

    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: FakeSQSClient())
    notifier = SQSJobNotifier("https://sqs.example.test/jobs")
    receipt = notifier.receive(wait_seconds=999)

    assert receipt is not None
    assert receipt.job_id == job_id
    assert receipt.tenant_id == "tenant-a"
    assert calls["WaitTimeSeconds"] == 20
    assert calls["VisibilityTimeout"] == 60

    with pytest.raises(ValueError, match="canonical UUID"):
        notifier.notify(job_id="not-a-uuid", tenant_id="tenant-a")
