from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QueueReceipt:
    receipt_handle: str
    job_id: str
    tenant_id: str


@dataclass(frozen=True)
class SQSJobNotifier:
    """At-least-once wake-up channel; PostgreSQL remains state authority."""

    queue_url: str
    endpoint_url: str | None = None

    def notify(self, *, job_id: str, tenant_id: str) -> None:
        import boto3
        from botocore.config import Config

        _validate_wakeup_identity(job_id, tenant_id)
        client = boto3.client(
            "sqs",
            endpoint_url=self.endpoint_url,
            config=Config(
                connect_timeout=3,
                read_timeout=25,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(
                {"schema_version": 1, "job_id": job_id, "tenant_id": tenant_id},
                separators=(",", ":"),
                sort_keys=True,
            ),
        )

    def receive(self, *, wait_seconds: int = 20) -> QueueReceipt | None:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "sqs",
            endpoint_url=self.endpoint_url,
            config=Config(
                connect_timeout=3,
                read_timeout=25,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        response: dict[str, Any] = client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=max(0, min(wait_seconds, 20)),
            VisibilityTimeout=60,
        )
        messages = response.get("Messages", [])
        if not messages:
            return None
        receipt_handle = str(messages[0].get("ReceiptHandle", ""))
        try:
            body = json.loads(str(messages[0].get("Body", "")))
            if (
                not isinstance(body, dict)
                or body.get("schema_version") != 1
                or not isinstance(body.get("job_id"), str)
                or not isinstance(body.get("tenant_id"), str)
            ):
                raise ValueError("Unsupported job wake-up payload")
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Invalid job wake-up payload") from exc
        if not receipt_handle:
            raise ValueError("Job wake-up lacks a receipt handle")
        _validate_wakeup_identity(body["job_id"], body["tenant_id"])
        return QueueReceipt(
            receipt_handle=receipt_handle,
            job_id=body["job_id"],
            tenant_id=body["tenant_id"],
        )

    def acknowledge(self, receipt: QueueReceipt) -> None:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "sqs",
            endpoint_url=self.endpoint_url,
            config=Config(
                connect_timeout=3,
                read_timeout=10,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        client.delete_message(
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt.receipt_handle,
        )


TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


def _validate_wakeup_identity(job_id: str, tenant_id: str) -> None:
    try:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ValueError("Job wake-up job_id must be a canonical UUID") from exc
    if not TENANT_PATTERN.fullmatch(tenant_id):
        raise ValueError("Job wake-up tenant_id is invalid")
