"""Tests for CloudWatch dashboard links and Logs Insights query contracts.

Dashboard links carry only a valid dashboard name. The separately displayed
call SID is entered into the dashboard's pattern-variable input.
"""

from __future__ import annotations

from pathlib import Path

from app.dashboard import (
    call_dashboard_query,
    call_dashboard_url,
    per_call_dashboard_name,
    stack_name_from_log_group,
)

_TEMPLATE = Path(__file__).resolve().parents[1] / "infra" / "template.yaml"


def test_url_targets_consolidated_dashboards_fragment_not_logs_insights():
    url = call_dashboard_url("CA-123", aws_region="us-east-1")
    # Opens the dashboards console fragment, scoped to the named dashboard.
    assert "#dashboards:name=" in url
    assert "ar-collections-nova-sonic-per-call" in url
    # A dashboard URL must not contain a Logs Insights query fragment.
    assert "logsV2:logs-insights" not in url
    assert "queryDetail" not in url


def test_url_name_segment_is_exactly_the_valid_dashboard_name():
    """The fragment's name segment contains only a valid dashboard name."""
    url = call_dashboard_url("CA00000000000000000000000000000001", aws_region="us-east-1")
    # Isolate the name token (everything after "name=" to the end of the URL).
    name_segment = url.split("#dashboards:name=", 1)[1]
    assert name_segment == "ar-collections-nova-sonic-per-call"
    # No leftover variable-override escapes anywhere in the URL.
    assert "$3F" not in url and "$3D" not in url
    assert "params" not in url
    assert "~(" not in url
    # Every character of the name segment is a VALID DashboardName character.
    assert all(ch.isalnum() or ch in "-_" for ch in name_segment)


def test_url_uses_region_and_derives_stack_from_log_group():
    url = call_dashboard_url(
        "CA-1",
        aws_region="eu-west-1",
        app_log_group="/ecs/my-demo-stack/app",
    )
    assert "region=eu-west-1" in url
    assert "my-demo-stack-per-call" in url


def test_url_is_pure_string_and_deterministic():
    a = call_dashboard_url("CA-SAME")
    b = call_dashboard_url("CA-SAME")
    assert a == b
    assert isinstance(a, str) and a.startswith("https://")


def test_stack_name_from_log_group_parsing():
    assert stack_name_from_log_group("/ecs/some-stack/app") == "some-stack"
    # Non-matching values fall back to the default stack name.
    assert stack_name_from_log_group(None) == "ar-collections-nova-sonic"
    assert stack_name_from_log_group("/weird/group") == "ar-collections-nova-sonic"


def test_per_call_dashboard_name():
    assert per_call_dashboard_name("foo") == "foo-per-call"
    assert per_call_dashboard_name() == "ar-collections-nova-sonic-per-call"


def test_query_helper_still_filters_by_call_sid_and_parses_logs():
    q = call_dashboard_query("CA-42", "/ecs/ar-collections-nova-sonic/app")
    assert "SOURCE '/ecs/ar-collections-nova-sonic/app'" in q
    assert "filter call_sid = 'CA-42'" in q
    assert "sort t_ms asc" in q
    # Surfaces the per-call observability facets.
    for field in ("ttft_ms", "input_tokens", "output_tokens", "total_tokens", "tool"):
        assert field in q


# --- Per-call dashboard widget query shapes --------------------------------
# Read the PerCallDashboard body as text because its CloudFormation !Sub
# substitutions are not JSON. Assertions cover the Logs Insights expressions.


def _per_call_dashboard_body() -> str:
    text = _TEMPLATE.read_text(encoding="utf-8")
    marker = "PerCallDashboard:"
    start = text.index(marker)
    # The next top-level resource/comment block after the dashboard bounds it.
    end = text.index("# --- CloudTrail", start)
    return text[start:end]


def test_ttft_and_turns_widget_counts_turn_complete_events_not_a_field():
    """Count turn_complete event rows rather than a field of that name."""
    body = _per_call_dashboard_body()
    # Count events rather than a nonexistent turn_complete field.
    assert "count(turn_complete)" not in body
    # The widget filters on the event_type VALUE and counts matching rows.
    assert "event_type = 'turn_complete' | stats" in body
    assert "count(*) as turns" in body
    # TTFT stats still live on the same (turn_complete) events.
    assert "avg(ttft_ms) as avg_ttft_ms" in body
    assert "avg(turn_latency_ms) as avg_response_wait_ms" in body


def test_transport_widget_reads_nested_summary_dot_paths_not_flat_fields():
    """Read call-end metrics from their nested summary object.

    CloudWatch discovers nested JSON as dot-paths, so the transport widget must
    reference ``summary.total_e2e_ms`` etc., not the flat names, which resolve
    to nothing and render the columns blank.
    """
    body = _per_call_dashboard_body()
    for path in (
        "summary.total_e2e_ms as total_e2e_ms",
        "summary.nova_total_ms as nova_total_ms",
        "summary.twilio_latency_ms as twilio_latency_ms",
        "summary.transport_delay_estimate_ms as transport_delay_estimate_ms",
    ):
        assert path in body, path
    # The transport widget still scopes to twilio + call_end_summary events.
    assert "event_type = 'call_end_summary'" in body
