"""Unit tests for app.code_interpreter.resolve_date_phrase.

The resolution program is MODEL-AUTHORED then executed in the sandbox, so these
tests inject BOTH:

  * a fake bedrock-runtime client whose ``converse`` returns a canned, generated
    Python program (proving the module asks the MODEL to write the code, and
    that it passes the phrase + reference date as arguments), and
  * a fake AgentCore CodeInterpreter client whose ``executeCode`` returns the
    printed ISO date (proving the module feeds the model's program to the
    sandbox and parses stdout).

No AWS call happens. The tests assert the wrapper prompts the model with the
phrase + reference date, feeds the returned program to executeCode with
language='python', parses the ISO date, returns None on isError / exception /
unparseable output, always calls stop(), and short-circuits an already-ISO
phrase before touching either client.
"""

from __future__ import annotations

from app import code_interpreter


class FakeBedrock:
    """Stub bedrock-runtime client returning a canned generated program.

    Records the converse kwargs so tests can assert the phrase + reference date
    were supplied to the model as arguments.
    """

    def __init__(self, program: str = "print('2024-06-03')") -> None:
        self.program = program
        self.calls: list[dict] = []
        self.raise_on_converse: Exception | None = None

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_on_converse is not None:
            raise self.raise_on_converse
        return {
            "output": {
                "message": {"content": [{"text": self.program}]},
            }
        }


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


def test_model_authors_program_fed_to_sandbox_and_iso_parsed():
    """The model WRITES the program; the sandbox EXECUTES it; ISO parsed out."""
    generated = (
        "from datetime import date, timedelta\n"
        "ref = date(2024, 5, 15)\n"
        "print((ref + timedelta(days=19)).isoformat())\n"
    )
    bedrock = FakeBedrock(program=generated)
    captured: list[FakeCodeInterpreter] = []

    def factory(region):
        c = FakeCodeInterpreter(region)
        captured.append(c)
        return c

    result = code_interpreter.resolve_date_phrase(
        "next month, the first Monday",
        reference_date="2024-05-15",
        region="us-east-1",
        bedrock_client=bedrock,
        client_factory=factory,
    )

    assert result == "2024-06-03"

    # The MODEL was prompted with the phrase + the reference date as arguments.
    assert bedrock.calls, "the model should have been asked to write the program"
    user_text = bedrock.calls[0]["messages"][0]["content"][0]["text"]
    assert "next month, the first Monday" in user_text
    assert "2024-05-15" in user_text

    # The sandbox ran the MODEL's program via executeCode(language='python').
    assert len(captured) == 1
    client = captured[0]
    assert client.region == "us-east-1"
    assert client.started is True
    assert client.stopped is True
    assert client.invocations, "invoke should have been called"
    method, params = client.invocations[0]
    assert method == "executeCode"
    assert params["language"] == "python"
    # The executed code is the model-authored snippet (wrapped in the harness),
    # NOT a hand-written parser shipped from app/.
    assert "ref + timedelta(days=19)" in params["code"]
    assert "model-generated date-resolution program" in params["code"]


def test_already_iso_phrase_short_circuits_without_model_or_sandbox():
    bedrock = FakeBedrock()
    called = {"n": 0}

    def factory(region):
        called["n"] += 1
        return FakeCodeInterpreter(region)

    result = code_interpreter.resolve_date_phrase(
        "2024-07-04",
        reference_date="2024-05-15",
        region="us-east-1",
        bedrock_client=bedrock,
        client_factory=factory,
    )

    assert result == "2024-07-04"
    # Neither the model nor the sandbox was touched.
    assert bedrock.calls == []
    assert called["n"] == 0


def test_returns_none_on_is_error_but_still_stops():
    bedrock = FakeBedrock()
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
        bedrock_client=bedrock,
        client_factory=factory,
    )

    assert result is None
    assert client_box["c"].stopped is True


def test_returns_none_when_unresolved_sentinel_printed():
    bedrock = FakeBedrock(program="print('UNRESOLVED')")

    def factory(region):
        c = FakeCodeInterpreter(region)
        c.stdout = "UNRESOLVED\n"
        return c

    result = code_interpreter.resolve_date_phrase(
        "whenever I feel like it",
        reference_date="2024-05-15",
        region="us-east-1",
        bedrock_client=bedrock,
        client_factory=factory,
    )
    assert result is None


def test_returns_none_and_stops_on_invoke_exception():
    bedrock = FakeBedrock()
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
        bedrock_client=bedrock,
        client_factory=factory,
    )

    # Never raises into the caller; session is still stopped.
    assert result is None
    assert client_box["c"].stopped is True


def test_returns_none_when_model_call_fails_without_starting_sandbox():
    """A failed code-generation call returns None and never starts a session."""
    bedrock = FakeBedrock()
    bedrock.raise_on_converse = RuntimeError("model boom")
    called = {"n": 0}

    def factory(region):
        called["n"] += 1
        return FakeCodeInterpreter(region)

    result = code_interpreter.resolve_date_phrase(
        "next Friday",
        reference_date="2024-05-15",
        region="us-east-1",
        bedrock_client=bedrock,
        client_factory=factory,
    )
    assert result is None
    # The model was attempted, but the sandbox was never started.
    assert bedrock.calls, "the model should have been attempted"
    assert called["n"] == 0


def test_empty_model_program_returns_none_without_sandbox():
    bedrock = FakeBedrock(program="   ")
    called = {"n": 0}

    def factory(region):
        called["n"] += 1
        return FakeCodeInterpreter(region)

    result = code_interpreter.resolve_date_phrase(
        "next Friday",
        reference_date="2024-05-15",
        region="us-east-1",
        bedrock_client=bedrock,
        client_factory=factory,
    )
    assert result is None
    assert called["n"] == 0


def test_strips_code_fences_from_model_output():
    """A model that wraps its code in ```python fences is tolerated."""
    fenced = "```python\nprint('2024-06-03')\n```"
    bedrock = FakeBedrock(program=fenced)
    captured: list[FakeCodeInterpreter] = []

    def factory(region):
        c = FakeCodeInterpreter(region)
        captured.append(c)
        return c

    result = code_interpreter.resolve_date_phrase(
        "next month, the first Monday",
        reference_date="2024-05-15",
        region="us-east-1",
        bedrock_client=bedrock,
        client_factory=factory,
    )
    assert result == "2024-06-03"
    code = captured[0].invocations[0][1]["code"]
    assert "```" not in code
    assert "print('2024-06-03')" in code


class ExecutingCodeInterpreter:
    """Stub AgentCore CodeInterpreter that ACTUALLY RUNS the model's program.

    Unlike :class:`FakeCodeInterpreter` (which returns a canned stdout), this
    stub executes the model-authored program handed to ``executeCode`` in an
    isolated local namespace and captures its stdout. This lets a test assert
    the date MATH is correct end-to-end (phrase -> program -> real stdout ->
    parsed ISO) without touching AWS, so it would fail if the resolution is
    reverted to a wrong program. Execution stays offline and uses only the
    standard library.
    """

    def __init__(self, region: str) -> None:
        self.region = region
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def invoke(self, method, params=None):
        import contextlib
        import io

        code = (params or {}).get("code", "")
        buf = io.StringIO()
        is_error = False
        try:
            with contextlib.redirect_stdout(buf):
                exec(compile(code, "<model-program>", "exec"), {"__name__": "__main__"})
        except Exception:  # noqa: BLE001 - surface as an interpreter error
            is_error = True
        return {
            "stream": [
                {
                    "result": {
                        "isError": is_error,
                        "content": [{"type": "text", "text": buf.getvalue()}],
                    }
                }
            ]
        }

    def stop(self):
        self.stopped = True


def test_second_tuesday_of_next_month_resolves_correctly_anchored():
    """Deterministic, OFFLINE proof of ASK 8: the model-authored program for
    'second tuesday of next month' anchored on 2024-05-15 resolves to the
    SECOND Tuesday of June 2024 = 2024-06-11. The FakeBedrock returns a real
    (model-style) program and the ExecutingCodeInterpreter runs it for real, so
    this fails if the resolution produces the wrong date.
    """
    # A realistic program the code-gen model would author for this phrase:
    # pick the month after the reference month, then the 2nd Tuesday.
    program = (
        "import calendar\n"
        "from datetime import date\n"
        "ref = date(2024, 5, 15)\n"
        "year = ref.year + (1 if ref.month == 12 else 0)\n"
        "month = 1 if ref.month == 12 else ref.month + 1\n"
        "tuesdays = [d for d in range(1, calendar.monthrange(year, month)[1] + 1)\n"
        "            if date(year, month, d).weekday() == calendar.TUESDAY]\n"
        "print(date(year, month, tuesdays[1]).isoformat())\n"
    )
    bedrock = FakeBedrock(program=program)
    captured: list[ExecutingCodeInterpreter] = []

    def factory(region):
        c = ExecutingCodeInterpreter(region)
        captured.append(c)
        return c

    result = code_interpreter.resolve_date_phrase(
        "second tuesday of next month",
        reference_date="2024-05-15",
        region="us-east-1",
        bedrock_client=bedrock,
        client_factory=factory,
    )

    assert result == "2024-06-11"
    # The phrase + reference date were supplied to the model as arguments.
    user_text = bedrock.calls[0]["messages"][0]["content"][0]["text"]
    assert "second tuesday of next month" in user_text
    assert "2024-05-15" in user_text
    # The sandbox actually ran the model's program and was cleaned up.
    assert captured and captured[0].started and captured[0].stopped


def test_empty_phrase_short_circuits_without_model_or_session():
    bedrock = FakeBedrock()
    called = {"n": 0}

    def factory(region):
        called["n"] += 1
        return FakeCodeInterpreter(region)

    result = code_interpreter.resolve_date_phrase(
        "   ",
        reference_date="2024-05-15",
        region="us-east-1",
        bedrock_client=bedrock,
        client_factory=factory,
    )
    assert result is None
    # No model call and no session is started for an empty phrase.
    assert bedrock.calls == []
    assert called["n"] == 0
