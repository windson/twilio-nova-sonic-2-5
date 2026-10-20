"""Tests for the per-call CloudWatch CONSOLIDATED dashboard deep-link builder.

FEAT-006 / D3: ``call_dashboard_url`` must now open the consolidated per-call
dashboard (``#dashboards:name=<stack>-per-call``) with the call_sid applied as
the dashboard pattern-variable override, NOT a bare ``logsV2:logs-insights``
Logs Insights deep-link. The Logs Insights query helper stays covered.
"""

from __future__ import annotations

from app.dashboard import (
    CALL_SID_TOKEN,
    call_dashboard_query,
    call_dashboard_url,
    per_call_dashboard_name,
    stack_name_from_log_group,
)


def test_url_targets_consolidated_dashboards_fragment_not_logs_insights():
    url = call_dashboard_url("CA-123", aws_region="us-east-1")
    # Opens the dashboards console fragment, scoped to the named dashboard.
    assert "#dashboards:name=" in url
    assert "ar-collections-nova-sonic-per-call" in url
    # The OLD logs-insights-only shape is gone.
    assert "logsV2:logs-insights" not in url
    assert "queryDetail" not in url


def test_url_includes_call_sid_scoping_token():
    url = call_dashboard_url("CA-ABC-999", aws_region="us-east-1")
    # The call_sid is carried as the dashboard-variable override value and the
    # variable token name is the pattern token the widgets filter on.
    assert "params" in url
    assert CALL_SID_TOKEN in url  # the pattern variable's token name
    assert "CA-ABC-999" in url  # the call_sid value (unreserved chars stay bare)


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
