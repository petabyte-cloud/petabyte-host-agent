"""Offline tests for the seller-agent telemetry (agent_telemetry.py).

Self-contained (the agent module has no platform imports). OTel is pointed at a bogus
endpoint so spans are created locally and export failures are swallowed — proving telemetry
NEVER blocks the agent. Covers: span exception preservation (no swallow / no double-yield),
degrade-safety, secret redaction, and W3C trace-context propagation from a job envelope.

Run: python agent_telemetry_test.py
"""
import json
import logging
import os

# Configure BEFORE importing agent_telemetry so module-level config picks it up.
os.environ["AGENT_TELEMETRY_ENABLED"] = "true"
os.environ["LOG_FORMAT"] = "json"
os.environ["LOG_REDACTION_ENABLED"] = "true"
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:4317"  # unreachable on purpose
os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "grpc"
os.environ["AGENT_ENV"] = "test"

import agent_telemetry as at  # noqa: E402

at.init(node_id="node_test_1")

_fail = 0


def ok(label, cond):
    global _fail
    print(("ok  " if cond else "FAIL") + "  " + label)
    if not cond:
        _fail += 1


# ---- finding A: a span NEVER swallows or replaces the caller's exception ----
# Regression for the "generator didn't stop after throw()" bug — the setup phase degrades
# (yield None) on its own failures, but once control is with the caller the ORIGINAL
# exception must propagate unchanged.
class _Boom(Exception):
    pass


_raised = "none"
try:
    with at.span("job.execute", job_id="job_1"):
        raise _Boom("agent-boom")
except _Boom as _e:
    _raised = ("boom", str(_e))
except Exception as _e:  # noqa: BLE001 — any other type is a failure of the property
    _raised = ("other", type(_e).__name__ + ":" + str(_e))
ok("a span re-raises the EXACT original exception from the caller's block "
   "(no swallow, no double-yield RuntimeError)",
   _raised == ("boom", "agent-boom"))

# ---- degrade-safe: a span + event with an unreachable exporter never raises ----
_raised2 = False
try:
    with at.span("agent.heartbeat"):
        at.event(at.EVENTS.HEARTBEAT, message="beat", gpu_util=42)
except Exception:  # noqa: BLE001
    _raised2 = True
ok("a span + event with an unreachable exporter never raises (degrade-safe)", not _raised2)

# ---- secret redaction in structured logs ----
captured = {}


class _Cap(logging.Handler):
    def emit(self, record):
        captured["line"] = at._JsonFmt().format(record)


logging.getLogger().addHandler(_Cap())
logging.getLogger().setLevel(logging.INFO)
at.event(at.EVENTS.JOB_RECEIVED, message="received", api_key="sk_live_SHOULDNOTAPPEAR",
         note="card 4242424242424242")
line = captured.get("line", "")
ok("a secret-shaped key is redacted in agent logs", "sk_live_SHOULDNOTAPPEAR" not in line)
ok("a PAN value pattern is redacted in agent logs", "4242424242424242" not in line)

# ---- W3C trace-context propagation: the agent joins the platform-side trace ----
if at.health()["otel_active"]:
    # A carrier as it would arrive on a job envelope from the platform.
    carrier = {}
    with at.span("producer.side"):
        from opentelemetry.propagate import inject
        inject(carrier)
    producer_tid = carrier.get("traceparent", "").split("-")[1] if carrier.get("traceparent") else None
    consumer_tid = None
    with at.span("job.execute", carrier=carrier):
        consumer_tid = at._ctx.get("trace_id")
    ok("the agent span joins the incoming (platform) trace via the job-envelope carrier",
       bool(producer_tid) and len(producer_tid) == 32 and producer_tid == consumer_tid)

    # ---- finding 2: a completed span's trace_id must NOT leak onto later inter-job events ----
    with at.span("job1.execute"):
        tid1 = at._ctx.get("trace_id")
    ok("agent: a span's trace_id is cleared when it ends (no cross-job contamination)",
       bool(tid1) and at._ctx.get("trace_id") is None)
    # An event emitted BETWEEN jobs (like the next job's JOB_RECEIVED, before its span) must
    # not inherit the previous job's trace_id.
    captured.clear()
    at.event(at.EVENTS.JOB_RECEIVED, message="next job claimed")
    doc2 = json.loads(captured.get("line", "") or "{}")
    ok("agent: an inter-job event is not attributed to the previous job's trace",
       "trace_id" not in doc2 and tid1 not in captured.get("line", ""))
else:
    ok("trace-context propagation (skipped: OTel SDK not installed)", True)
    ok("agent: trace_id cleared after span (skipped)", True)
    ok("agent: inter-job event isolation (skipped)", True)

print(f"\n=== agent telemetry: {'0 failures' if _fail == 0 else str(_fail) + ' FAILED'} ===")
raise SystemExit(1 if _fail else 0)
