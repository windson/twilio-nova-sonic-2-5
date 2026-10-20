"""Unit tests for app.code_interpreter.resolve_date_phrase.

An injected fake CodeInterpreter client stands in for the AgentCore sandbox so
NO AWS call happens. The tests assert the wrapper ships a Python program
(language='python') that references the phrase + reference date, parses the ISO
date the sandbox prints, returns None on isError, and always calls stop().
"""

from __future__ import annotations

from app import code_interpreter


class FakeCodeInterpreter:
    """Stub AgentCore CodeInterpreter client.

    Records the invoke params and returns a canned executeCode stream. The
    ``stdout`` text and ``is_error`` flag are configurable per test.
    """

    def __init__(self, region: str) -> None:
        self.region = region
        self.started = False
        self.stopped = False
        self.invocations: list[tuple[str, dict]] = []
        self.stdout = "2024-06-03\n"
        self.is_error = False
        self.raise_on_invoke: Exception | None = None

    def start(self):
        self.started = True

    def invoke(self, method, params=None):
        self.invocations.append((method, params or {}))
        if self.raise_on_invoke is not None:
            raise self.raise_on_invoke
        return {
            "stream": [
                {
                    "result": {
                        "isError": self.is_error,
                        "content": [{"type": "text", "text": self.stdout}],
                    }
                }
            ]
        }

    def stop(self):
        self.stopped = True


def test_resolve_date_phrase_sends_python_program_and_parses_iso():
    captured: list[FakeCodeInterpreter] = []

    def factory(region):
        c = FakeCodeInterpreter(region)
        captured.append(c)
        return c

    result = code_interpreter.resolve_date_phrase(
        "next month, the first Monday",
        reference_date="2024-05-15",
        region="us-east-1",
        client_factory=factory,
    )

    assert result == "2024-06-03"
    assert len(captured) == 1
    client = captured[0]
    assert client.region == "us-east-1"
    assert client.started is True
    # Always stops the session.
    assert client.stopped is True

    # executeCode was invoked with language='python' and a program referencing
    # both the phrase and the reference date.
    assert client.invocations, "invoke should have been called"
    method, params = client.invocations[0]
    assert method == "executeCode"
    assert params["language"] == "python"
    program = params["code"]
    assert "next month, the first Monday" in program
    assert "2024-05-15" in program


def test_resolve_date_phrase_returns_none_on_is_error_but_still_stops():
    client_box = {}

    def factory(region):
        c = FakeCodeInterpreter(region)
        c.is_error = True
        c.stdout = "Traceback (most recent call last): ..."
        client_box["c"] = c
        return c

    result = code_interpreter.resolve_date_phrase(
        "in two weeks",
        reference_date="2024-05-15",
        region="us-east-1",
        client_factory=factory,
    )

    assert result is None
    assert client_box["c"].stopped is True


def test_resolve_date_phrase_returns_none_when_unresolved_sentinel_printed():
    def factory(region):
        c = FakeCodeInterpreter(region)
        c.stdout = "UNRESOLVED\n"
        return c

    result = code_interpreter.resolve_date_phrase(
        "whenever I feel like it",
        reference_date="2024-05-15",
        region="us-east-1",
        client_factory=factory,
    )
    assert result is None


def test_resolve_date_phrase_returns_none_and_stops_on_invoke_exception():
    client_box = {}

    def factory(region):
        c = FakeCodeInterpreter(region)
        c.raise_on_invoke = RuntimeError("sandbox boom")
        client_box["c"] = c
        return c

    result = code_interpreter.resolve_date_phrase(
        "next Friday",
        reference_date="2024-05-15",
        region="us-east-1",
        client_factory=factory,
    )

    # Never raises into the caller; session is still stopped.
    assert result is None
    assert client_box["c"].stopped is True


def test_resolve_date_phrase_empty_phrase_short_circuits_without_session():
    called = {"n": 0}

    def factory(region):
        called["n"] += 1
        return FakeCodeInterpreter(region)

    result = code_interpreter.resolve_date_phrase(
        "   ", reference_date="2024-05-15", region="us-east-1", client_factory=factory
    )
    assert result is None
    # No session is started for an empty phrase.
    assert called["n"] == 0
