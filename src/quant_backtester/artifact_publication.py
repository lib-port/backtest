from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable

from quant_backtester.persistence.db import Database
from quant_backtester.results import ArtifactStore

logger = logging.getLogger(__name__)


class ArtifactPublicationError(RuntimeError):
    pass


def publish_pending_artifacts(
    database: Database,
    store: ArtifactStore,
    *,
    run_id: str,
    tenant_id: str,
    ensure_active: Callable[[], None] | None = None,
) -> None:
    """Publish exact outbox bytes, then transactionally record their descriptor."""
    pending = database.pending_artifact_publications(
        run_id,
        tenant_id=tenant_id,
        due_only=True,
    )
    for publication in pending:
        if ensure_active is not None:
            ensure_active()
        digest = hashlib.sha256(publication.payload).hexdigest()
        if digest != publication.content_hash or len(publication.payload) != publication.size_bytes:
            raise RuntimeError("Artifact outbox payload failed its integrity check")
        try:
            descriptor = store.write_payload(
                run_id=publication.run_id,
                kind=publication.kind,
                payload=publication.payload,
            )
            if ensure_active is not None:
                ensure_active()
            database.mark_artifact_published(
                publication.id,
                tenant_id=tenant_id,
                uri=descriptor.uri,
                content_hash=descriptor.content_hash,
                size_bytes=descriptor.size_bytes,
            )
        except Exception as exc:
            try:
                database.record_artifact_publication_failure(
                    publication.id,
                    tenant_id=tenant_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                logger.exception(
                    "Failed to record artifact publication error",
                    extra={"event": {"run_id": run_id, "publication_id": publication.id}},
                )
            raise ArtifactPublicationError(f"Artifact publication failed for run {run_id}") from exc
