"""Per-call CloudWatch dashboard deep-link builder (ASK 4).

This module builds, as a PURE STRING from config only (no live AWS call on the
request path), a consolidated CloudWatch **Logs Insights** console deep-link
scoped to a single ``call_sid``. Opening it shows the per-call observability
view -- TTFT, turns, token usage, barge-ins, tool invocations, transport and a
call timeline -- all derived from the one-JSON-object-per-line app logs the
service emits to the app log group (``/ecs/<stack>/app`` by default, see
``app/metrics.py`` ``_JsonLineFormatter`` + ``CallMetrics._emit``).

Why a Logs Insights deep-link and not a per-call ``AWS::CloudWatch::Dashboard``
resource: a dashboard-per-call cannot be a static CloudFormation resource (the
``call_sid`` values are unbounded and only known at runtime). A console deep-
link whose Logs Insights query filters by ``call_sid`` against the configured
log group + region gives the same consolidated, call-scoped view with no new
IAM and no live AWS call when the API serves the record. The STATIC base
dashboard (aggregate observability) stays in IaC (infra/template.yaml).

Encoding: the CloudWatch console encodes the ``logsV2:logs-insights`` fragment
with its own escape scheme -- ``?`` -> ``$3F``, ``=`` -> ``$3D``, and the
``queryDetail`` value is a RISON-like token tree using ``~`` + ``'`` where each
literal string is URL-percent-encoded with ``%`` further rewritten to the
console's ``*`` escape (e.g. a space ``%20`` becomes ``*20``). This matches the
URL the CloudWatch console itself produces for a saved Logs Insights query and
is verified against the AWS "Dashboard Body Structure and Syntax" / Logs
Insights documentation (log widget ``properties.query`` with the
``SOURCE '<log-group>' | fields ... | filter ... | stats ... | sort ...``
grammar).
"""

from __future__ import annotations

DEFAULT_REGION = "us-east-1"
DEFAULT_APP_LOG_GROUP = "/ecs/ar-collections-nova-sonic/app"


def _console_token(value: str) -> str:
    """Percent-encode ``value`` then rewrite ``%`` to the console ``*`` escape.

    The CloudWatch console RISON-like fragment encodes literal strings by URL-
    percent-encoding them and replacing the ``%`` prefix with ``*`` (so a space
    is ``*20``, a slash ``*2F``). Only unreserved characters are left bare.
    """
    out: list[str] = []
    for ch in value:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("".join(f"*{b:02X}" for b in ch.encode("utf-8")))
    return "".join(out)


def call_dashboard_query(call_sid: str, app_log_group: str) -> str:
    """Return the CloudWatch Logs Insights query string for one call.

    The query parses the app's JSON-line logs (fields are already top-level
    because CloudWatch auto-discovers JSON keys), filters to the single
    ``call_sid``, and orders by ``t_ms`` to produce a readable call timeline
    surfacing TTFT (``ttft_ms``), turns (``turn_complete`` / ``turn_latency_ms``
    / ``duration_ms``), token usage (``input_tokens`` / ``output_tokens`` /
    ``total_tokens``), barge-ins (``event_type='barge_in'``), tool invocations
    (``event_type='tool_invocation'`` + ``tool``) and the per-event timeline.
    """
    # Keep on a single logical line; the console accepts the literal pipes.
    return (
        f"SOURCE '{app_log_group}'"
        f" | fields t_ms, event_type, ttft_ms, turn_latency_ms, duration_ms,"
        f" tool, role, input_tokens, output_tokens, total_tokens, message"
        f" | filter call_sid = '{call_sid}'"
        f" | sort t_ms asc"
        f" | limit 1000"
    )


def call_dashboard_url(
    call_sid: str,
    aws_region: str | None = None,
    app_log_group: str | None = None,
) -> str:
    """Build the per-call CloudWatch Logs Insights dashboard deep-link.

    Pure string construction from config (``aws_region`` + ``app_log_group``);
    no AWS API call. The returned URL opens a consolidated CloudWatch view
    scoped to ``call_sid`` against the configured app log group.
    """
    region = aws_region or DEFAULT_REGION
    log_group = app_log_group or DEFAULT_APP_LOG_GROUP
    query = call_dashboard_query(call_sid, log_group)

    # RISON-like queryDetail object the console understands:
    #   ~(end~0~start~-3600~timeType~'RELATIVE~unit~'seconds
    #     ~editorString~'<query>~source~(~'<log-group>))
    # Each literal string is console-token encoded.
    query_tok = _console_token(query)
    group_tok = _console_token(log_group)
    detail = (
        "~(end~0~start~-3600~timeType~'RELATIVE~unit~'seconds"
        f"~editorString~'{query_tok}"
        f"~source~(~'{group_tok}))"
    )

    base = f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
    return f"{base}#logsV2:logs-insights$3FqueryDetail$3D{detail}"
