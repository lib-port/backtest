from __future__ import annotations

import json
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

        client = boto3.client("sqs", endpoint_url=self.endpoint_url)
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

        client = boto3.client("sqs", endpoint_url=self.endpoint_url)
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
        return QueueReceipt(
            receipt_handle=receipt_handle,
            job_id=body["job_id"],
            tenant_id=body["tenant_id"],
        )

    def acknowledge(self, receipt: QueueReceipt) -> None:
        import boto3

        client = boto3.client("sqs", endpoint_url=self.endpoint_url)
        client.delete_message(
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt.receipt_handle,
        )
