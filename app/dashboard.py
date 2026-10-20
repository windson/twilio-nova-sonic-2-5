"""Per-call CloudWatch **consolidated dashboard** deep-link builder (ASK 4 / D3).

This module builds, as a PURE STRING from config only (no live AWS call on the
request path), a deep-link that OPENS the consolidated CloudWatch dashboard
provisioned in IaC (``infra/template.yaml`` -> ``PerCallDashboard``, named
``<stack>-per-call``) with its widgets scoped to a single ``call_sid``.

Why a consolidated dashboard (not a bare Logs Insights link): the user wants a
multi-widget, OTEL-style consolidated view per call -- per-turn timeline, TTFT +
turns, token usage, barge-ins, tool calls, Twilio/transport -- not a single logs
stream. That view is a STATIC ``AWS::CloudWatch::Dashboard`` (several Logs
Insights ``log`` widgets over the app log group ``/ecs/<stack>/app``). The
widgets filter on a **pattern dashboard variable** whose token (``CALL_SID_TOKEN``)
appears inside every widget query; a pattern variable rewrites that token across
the whole dashboard JSON, so overriding it with one ``call_sid`` scopes every
widget to that call at once.

Scoping mechanism (verified against the AWS "Dashboard Body Structure and
Syntax" / "Create flexible dashboards with dashboard variables" docs): the
dashboard declares a ``variables`` entry of ``type: pattern`` whose ``pattern``
is the literal ``CALL_SID_TOKEN``. The console applies a variable override
carried in the dashboard URL fragment after the dashboard name, serialized as
the console's RISON-like ``~(...)`` token tree. Opening
``#dashboards:name=<dashboard>`` with that token pre-selects the ``call_sid`` so
the log widgets load already filtered to the call. No live AWS call, no new IAM.

Fallback: if the consolidated dashboard has not been deployed yet (older stack),
the link still resolves to the dashboards console for the named dashboard; the
operator can type the ``call_sid`` into the dashboard's variable input box. The
builder never raises and never performs a network call.

Encoding: dashboard deep-links use ``$`` escapes in the fragment (``?`` ->
``$3F``, ``=`` -> ``$3D``) and encode literal strings in the RISON-like token
tree by URL-percent-encoding them with ``%`` rewritten to the console ``*``
escape (a space becomes ``*20``). Unreserved chars are left bare.
"""

from __future__ import annotations

DEFAULT_REGION = "us-east-1"
DEFAULT_APP_LOG_GROUP = "/ecs/ar-collections-nova-sonic/app"
DEFAULT_STACK_NAME = "ar-collections-nova-sonic"

# The literal token the per-call dashboard's log-widget queries filter on; the
# pattern dashboard variable rewrites it across the dashboard JSON. MUST match
# the ``pattern`` declared in infra/template.yaml (PerCallDashboard variables).
CALL_SID_TOKEN = "CALL_SID_TOKEN"


def stack_name_from_log_group(app_log_group: str | None) -> str:
    """Derive the stack name from the app log group ``/ecs/<stack>/app``.

    The app log group name embeds the stack name (see infra/template.yaml
    ``AppLogGroup`` -> ``!Sub "/ecs/${AWS::StackName}/app"``), so no extra
    config field is needed to name the per-call dashboard. Falls back to
    :data:`DEFAULT_STACK_NAME` for any value that does not match the pattern.
    """
    if not app_log_group:
        return DEFAULT_STACK_NAME
    parts = app_log_group.strip("/").split("/")
    if len(parts) == 3 and parts[0] == "ecs" and parts[2] == "app" and parts[1]:
        return parts[1]
    return DEFAULT_STACK_NAME


def per_call_dashboard_name(stack_name: str | None = None) -> str:
    """Return the consolidated per-call dashboard name (``<stack>-per-call``).

    MUST match ``DashboardName`` of ``PerCallDashboard`` in infra/template.yaml
    (``!Sub "${AWS::StackName}-per-call"``).
    """
    return f"{stack_name or DEFAULT_STACK_NAME}-per-call"


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
    """Return a per-call Logs Insights query filtered to one ``call_sid``.

    Kept for the UI/backend to display what the per-call dashboard shows and to
    keep the query grammar under test. It parses the app's JSON-line logs
    (CloudWatch auto-discovers JSON keys as top-level fields), filters to the
    single ``call_sid`` and orders by ``t_ms`` to surface the per-event timeline
    with TTFT (``ttft_ms``), turns (``turn_latency_ms`` / ``duration_ms``),
    token usage (``input_tokens`` / ``output_tokens`` / ``total_tokens``),
    barge-ins (``event_type='barge_in'``) and tool invocations
    (``event_type='tool_invocation'`` + ``tool``).
    """
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
    stack_name: str | None = None,
) -> str:
    """Build the deep-link that opens the CONSOLIDATED per-call dashboard.

    Pure string construction from config (region + stack name); no AWS API
    call. The returned URL opens ``#dashboards:name=<stack>-per-call`` with the
    dashboard's pattern variable overridden to ``call_sid`` via the console's
    RISON-like URL token, so every log widget loads already scoped to the call.
    ``app_log_group`` is accepted for signature parity with the client mirror
    (the log group is baked into the dashboard widgets at deploy time).
    """
    region = aws_region or DEFAULT_REGION
    stack = stack_name or stack_name_from_log_group(app_log_group)
    name = per_call_dashboard_name(stack)

    # Dashboard-variable override token tree the console reads from the URL:
    #   ~(~(name~'CALL_SID_TOKEN~value~'<call_sid>))
    # name = the pattern variable's token; value = the call_sid to scope to.
    sid_tok = _console_token(call_sid)
    token_tok = _console_token(CALL_SID_TOKEN)
    variables = f"~(~(name~'{token_tok}~value~'{sid_tok}))"
    name_tok = _console_token(name)

    base = f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
    # dashboards fragment with the name and the variable override applied; the
    # $3F/$3D escapes carry the ?params=<token-tree> the console consumes.
    return (
        f"{base}#dashboards:name={name_tok}"
        f"$3Fparams$3D{variables}"
    )
