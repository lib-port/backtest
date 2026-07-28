from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id",
    default=None,
)
tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_id",
    default=None,
)
job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "job_id",
    default=None,
)
run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "run_id",
    default=None,
)


@contextmanager
def log_context(
    *,
    request_id: str | None = None,
    tenant_id: str | None = None,
    job_id: str | None = None,
    run_id: str | None = None,
) -> Iterator[None]:
    tokens: list[tuple[contextvars.ContextVar[str | None], contextvars.Token[str | None]]] = []
    for variable, value in (
        (request_id_var, request_id),
        (tenant_id_var, tenant_id),
        (job_id_var, job_id),
        (run_id_var, run_id),
    ):
        if value is not None:
            tokens.append((variable, variable.set(value)))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def current_trace_ids() -> tuple[str | None, str | None]:
    try:
        from opentelemetry import trace
    except ImportError:
        return None, None
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"
