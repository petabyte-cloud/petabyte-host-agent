"""Lean, dependency-optional telemetry for the Petabyte seller agent.

Self-contained (no platform imports): structured JSON logs with redaction, optional
OpenTelemetry tracing, and W3C trace-context propagation from the job envelope so a job's
execution on the seller node joins the SAME trace that started in the buyer's browser.

Degrade-safe by construction (non-negotiable): if opentelemetry is not installed, the
collector is unreachable, or AGENT_TELEMETRY_ENABLED=false, everything becomes a no-op and
the agent keeps polling, executing, and uploading results. Telemetry NEVER blocks work.
Secrets and full workload inputs are never logged.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "petabyte-seller-agent")
ENVIRONMENT = (os.environ.get("DEPLOYMENT_ENVIRONMENT")
               or os.environ.get("AGENT_ENV") or "production").strip()
RELEASE = (os.environ.get("PETABYTE_AGENT_VERSION")
           or os.environ.get("RELEASE_VERSION") or "dev").strip()
_ENABLED = os.getenv("AGENT_TELEMETRY_ENABLED", "true").strip().lower() == "true"
_OTLP = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
_REDACT = os.getenv("LOG_REDACTION_ENABLED", "true").strip().lower() != "false"
_JSON = os.getenv("LOG_FORMAT", "json").strip().lower() == "json"

_SENSITIVE = ("secret", "password", "token", "api_key", "apikey", "private_key",
              "authorization", "cookie", "credential", "cvc", "card")
_PATTERNS = [re.compile(r"sk_(live|test)_[A-Za-z0-9]+"),
             re.compile(r"whsec_[A-Za-z0-9]+"),
             re.compile(r"Bearer\s+[A-Za-z0-9._\-]+"),
             re.compile(r"\b\d{13,19}\b")]


class EVENTS:
    STARTUP = "agent.startup"
    ENROLLED = "agent.enrolled"
    AUTH = "agent.auth"
    HEARTBEAT = "agent.heartbeat"
    HEARTBEAT_MISSED = "agent.heartbeat.missed"
    GPU_DETECTED = "agent.gpu.detected"
    JOB_RECEIVED = "job.received"
    JOB_EXECUTION_STARTED = "job.execution.started"
    JOB_EXECUTION_COMPLETED = "job.execution.completed"
    JOB_EXECUTION_FAILED = "job.execution.failed"
    CONTAINER_STARTED = "gpu.container.started"
    RESULT_UPLOADED = "result.uploaded"
    RECONNECTED = "agent.reconnected"
    SHUTDOWN = "agent.shutdown"


def _mask(s: str) -> str:
    if not _REDACT or not isinstance(s, str):
        return s
    for p in _PATTERNS:
        s = p.sub("«redacted»", s)
    return s


def _redact(obj, depth=0):
    if not _REDACT or depth > 5:
        return obj
    if isinstance(obj, dict):
        return {k: ("«redacted»" if isinstance(k, str) and any(t in k.lower() for t in _SENSITIVE)
                    else _redact(v, depth + 1)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v, depth + 1) for v in obj][:100]
    if isinstance(obj, str):
        return _mask(obj)
    return obj


_ctx: dict = {}

# Standard LogRecord attribute names, computed ONCE (not per log line). Anything on a
# record that is NOT in this set is a caller-supplied structured `extra` field.
_STD_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys())


class _JsonFmt(logging.Formatter):
    def format(self, record):
        base = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc)
                         .isoformat().replace("+00:00", "Z"),
            "level": record.levelname, "message": _mask(record.getMessage()),
            "service": SERVICE_NAME, "environment": ENVIRONMENT, "release": RELEASE,
        }
        # Snapshot the shared context before merging so a concurrent bind()/init() can't
        # mutate it mid-iteration ("dictionary changed size during iteration").
        base.update(dict(_ctx))
        for k, v in record.__dict__.items():
            if k not in base and k not in _STD_RECORD_ATTRS:
                base[k] = v
        return json.dumps(_redact(base), default=str, ensure_ascii=False)


def configure_logging():
    root = logging.getLogger()
    root.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
    h = logging.StreamHandler()
    h.setFormatter(_JsonFmt() if _JSON else logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.handlers = [h]


_log = logging.getLogger("petabyte.agent")
_tracer = None
_otel_ok = False


def init(**resource_ids):
    """Initialise agent telemetry. Never raises; degrades to logging-only."""
    global _tracer, _otel_ok
    for k, v in resource_ids.items():
        if v is not None:
            _ctx[k] = str(v)
    configure_logging()
    if not (_ENABLED and _OTLP):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        if os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").startswith("http"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        else:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        prov = TracerProvider(resource=Resource.create({
            "service.name": SERVICE_NAME, "service.namespace": "petabyte",
            "service.version": RELEASE, "deployment.environment": ENVIRONMENT,
            "petabyte.host_role": "gpu"}))
        prov.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=_OTLP, timeout=int(os.getenv(
                "OBSERVABILITY_EXPORT_TIMEOUT_SECONDS", "5"))),
            max_queue_size=int(os.getenv("OBSERVABILITY_QUEUE_SIZE", "2048"))))
        trace.set_tracer_provider(prov)
        _tracer = trace.get_tracer("petabyte-agent")
        _otel_ok = True
    except Exception as e:  # noqa: BLE001
        _log.warning("agent OTel init skipped", extra={"event_name": "otel.init.skipped",
                                                       "reason": str(e)})


def bind(**kw):
    for k, v in kw.items():
        if v is not None:
            _ctx[k] = str(v)


def event(name, **fields):
    try:
        _log.info(fields.pop("message", name), extra={"event_name": name, **fields})
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def span(name, carrier=None, **attrs):
    """Span parented on the job envelope's W3C trace context (carrier), joining the
    platform-side trace. No-op if OTel is inactive; never swallows the caller's exception."""
    if not _otel_ok or _tracer is None:
        yield None
        return
    # Snapshot the trace_id currently in the shared context so we can RESTORE it when this
    # span ends. Without this, a completed span's trace_id lingers in the module-level
    # context and the NEXT job's pre-span events (e.g. JOB_RECEIVED) are wrongly attributed
    # to the previous job's trace.
    _prev_trace = _ctx.get("trace_id")
    # SETUP phase — degrade (yield None) only on a tracer/setup failure, BEFORE yielding.
    try:
        from opentelemetry import trace
        parent = None
        if carrier:
            with contextlib.suppress(Exception):
                from opentelemetry.propagate import extract
                parent = extract(carrier)
        span_cm = _tracer.start_as_current_span(name, context=parent)
        sp = span_cm.__enter__()
        for k, v in _redact(attrs).items():
            with contextlib.suppress(Exception):
                sp.set_attribute(k, v if isinstance(v, (str, bool, int, float)) else str(v))
        sctx = sp.get_span_context()
        _ctx["trace_id"] = format(sctx.trace_id, "032x")
    except Exception:  # noqa: BLE001 — a broken tracer must not break the agent
        with contextlib.suppress(Exception):
            span_cm.__exit__(None, None, None)  # noqa: F821
        yield None
        return
    # YIELDED phase — a caller exception is theirs: record it and re-raise the EXACT
    # original exception (never a second yield -> no "generator didn't stop after throw()").
    try:
        yield sp
    except Exception as exc:
        with contextlib.suppress(Exception):
            sp.record_exception(exc)
            sp.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
        raise
    finally:
        # Restore the prior trace_id (or drop it) so no later event inherits this span's
        # trace once it has ended.
        if _prev_trace is None:
            _ctx.pop("trace_id", None)
        else:
            _ctx["trace_id"] = _prev_trace
        with contextlib.suppress(Exception):
            span_cm.__exit__(*sys.exc_info())


def health():
    return {"enabled": _ENABLED, "otel_active": _otel_ok,
            "endpoint_configured": bool(_OTLP), "service": SERVICE_NAME}
