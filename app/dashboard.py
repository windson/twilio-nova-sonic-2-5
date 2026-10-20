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

Scoping mechanism: the dashboard declares a ``variables`` entry of
``type: pattern`` whose ``pattern`` is the literal ``CALL_SID_TOKEN``. Every log
widget's query embeds that token, so entering a ``call_sid`` in the dashboard's
``CALL_SID`` input box rewrites the token across the whole dashboard JSON and
scopes every widget to that call at once.

Deep-link grammar (BUG 2 fix, verified against the AWS CloudFormation docs for
``AWS::CloudWatch::Dashboard`` and the CloudWatch console dashboards fragment):
the ONLY documented, reliable deep-link to open a dashboard by name is

    https://<region>.console.aws.amazon.com/cloudwatch/home?region=<region>#dashboards:name=<DashboardName>

where the ``name`` segment contains ONLY the dashboard name. ``DashboardName``
itself may contain only alphanumerics, ``-`` and ``_`` (the console validates
this). The PREVIOUS builder fused a RISON ``$3Fparams$3D~(...)`` dashboard-
variable-override blob directly onto the name segment; because nothing
terminated the name token, the console parsed
``ar-collections-nova-sonic-per-call$3Fparams$3D...`` as the DashboardName and
rejected it with "The value for field DashboardName contains invalid
characters. It can only contain alphanumerics, dash (-) and underscore (_)."

AWS does not document a stable URL-fragment syntax for pre-filling a dashboard
PATTERN variable, so (per the fix guidance) we emit the clean, documented
named-dashboard link that opens the correct dashboard with NO invalid-name
error; the operator types the ``call_sid`` into the dashboard's ``CALL_SID``
input box (the dashboard then scopes every widget to that call). The builder is
a pure string construction -- no live AWS call, no new IAM -- and never raises.
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
    call. Returns the documented named-dashboard deep-link

        .../cloudwatch/home?region=<region>#dashboards:name=<stack>-per-call

    whose fragment ``name`` segment is EXACTLY the dashboard name (valid
    DashboardName chars only: alphanumerics, ``-``, ``_``) -- nothing is fused
    onto it, so the console no longer rejects it with "DashboardName contains
    invalid characters" (BUG 2). The ``call_sid`` is still returned to the
    caller (and used in :func:`call_dashboard_query`) so the UI can show it and
    the operator can paste it into the dashboard's ``CALL_SID`` input box to
    scope every widget to the call. ``app_log_group`` is accepted for signature
    parity with the client mirror (the log group is baked into the dashboard
    widgets at deploy time).
    """
    region = aws_region or DEFAULT_REGION
    stack = stack_name or stack_name_from_log_group(app_log_group)
    name = per_call_dashboard_name(stack)

    base = f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
    # dashboards fragment: the name segment is the bare, valid dashboard name
    # and nothing else, so it terminates cleanly and the console accepts it.
    return f"{base}#dashboards:name={name}"
