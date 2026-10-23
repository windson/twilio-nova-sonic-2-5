"""CloudWatch per-call dashboard links and query construction.

The infrastructure template provisions ``<stack>-per-call`` with Logs Insights
widgets for response timing, token usage, interruptions, tools, and transport.
Every widget query contains ``CALL_SID_TOKEN``. The dashboard's pattern variable
replaces that token with the call SID entered by the operator.

Links use the documented named-dashboard fragment::

    https://<region>.console.aws.amazon.com/cloudwatch/home?region=<region>#dashboards:name=<DashboardName>

The fragment contains only the dashboard name. AWS does not document a stable
URL syntax for prefilling its pattern variable, so the UI displays the call SID
for the operator to enter in the dashboard. All helpers construct strings from
configuration and perform no AWS requests.
"""

from __future__ import annotations

DEFAULT_REGION = "us-east-1"
DEFAULT_APP_LOG_GROUP = "/ecs/ar-collections-nova-sonic/app"
DEFAULT_STACK_NAME = "ar-collections-nova-sonic"

# The literal token the per-call dashboard's log-widget queries filter on; the
# pattern dashboard variable rewrites it across the dashboard JSON. MUST match
# The ``pattern`` declared in infra/template.yaml (PerCallDashboard variables).
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

    The fragment contains only valid dashboard-name characters: alphanumerics,
    ``-``, and ``_``. The operator enters the separately displayed ``call_sid``
    into the dashboard's ``CALL_SID`` input to scope all widgets. The parameter
    is retained for parity with the client helper; this named-dashboard URL
    itself does not encode the call SID. ``app_log_group`` supplies the stack
    name used by the infrastructure template.
    """
    region = aws_region or DEFAULT_REGION
    stack = stack_name or stack_name_from_log_group(app_log_group)
    name = per_call_dashboard_name(stack)

    base = f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
    # dashboards fragment: the name segment is the bare, valid dashboard name
    # and nothing else, so it terminates cleanly and the console accepts it.
    return f"{base}#dashboards:name={name}"
