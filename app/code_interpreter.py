"""AWS Bedrock AgentCore Code Interpreter wrapper (off-path date resolution).

This module is the REAL code interpreter that replaces the former hand-written
``_resolve_relative_date`` date parser. The key property, per the user's
explicit intent, is that the resolution program is **authored by a model on the
fly** and then **executed in the sandbox** - this module does NOT hand-write the
date math.

Flow of :func:`resolve_date_phrase`:

1. A Bedrock TEXT model (the existing ``EXTRACTION_MODEL_ID`` /
   ``Settings.extraction_model_id``, OpenAI GPT-6 Luna) is
   prompted via the Bedrock Converse API - exactly like
   :func:`app.postcall._classify` - to GENERATE a short Python program. The
   prompt supplies ONLY the arguments (the debtor's natural-language phrase +
   the reference/"today" date); the model reasons from today's date and writes
   the Python that computes the concrete calendar date, printing ONLY the
   resolved ``YYYY-MM-DD`` (or the ``UNRESOLVED`` sentinel).
2. The model-generated program is EXECUTED in the AWS Bedrock AgentCore Code
   Interpreter sandbox (``bedrock_agentcore.tools.code_interpreter_client``)
   via ``start()`` -> ``invoke("executeCode", {language, code})`` -> ``stop()``.
3. The printed ISO date is parsed back out of stdout.

A labelled source wrapper identifies the model-generated snippet. The output
validator accepts only one valid ISO date on or after the reference date;
missing, ambiguous, past or malformed output remains unresolved.

Latency note: a Code Interpreter session costs roughly ~1.75s to start plus
~0.13s to execute (~1.9s total), and the extra model Converse call adds more,
so this MUST only ever be called OFF the real-time audio/turn path. The single
caller is :func:`app.postcall.extract_and_apply`, which already runs post-call
as a fire-and-forget asyncio task. The in-call tool merely captures the raw
phrase.

Both clients are injectable so tests never touch AWS: ``bedrock_client`` for
the code-generation model and ``client_factory`` for the sandbox. The wrapper
is fully defensive: any exception, an ``isError`` result, or unparseable output
returns ``None`` (it never raises into the caller), and the session is always
stopped in a ``finally`` block. An already-ISO phrase short-circuits before
both the model and the sandbox.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from app.date_resolver import (
    payment_date_rejection, phrase_clarification_reasons, record_diagnostic, validate_payment_date,
)
from app.config import DEFAULT_EXTRACTION_MODEL_ID

logger = logging.getLogger(__name__)

# Managed Code Interpreter identifier (no CreateCodeInterpreter needed).
DEFAULT_CODE_INTERPRETER_ID = "aws.codeinterpreter.v1"

# Default TEXT model used to GENERATE the resolution program (mirrors
# Settings.extraction_model_id / EXTRACTION_MODEL_ID).
DEFAULT_CODE_GEN_MODEL_ID = DEFAULT_EXTRACTION_MODEL_ID

# Sentinel the generated program prints when it cannot resolve the phrase.
_UNRESOLVED = "UNRESOLVED"

_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Instruction driving the model to WRITE (not us) the date-resolution Python.
# The model reasons from today's date and the phrase and emits a self-contained
# program. It must print ONLY the resolved date (or the sentinel).
_CODE_GEN_SYSTEM = (
    "You are a Python programmer. Write a SHORT, self-contained Python 3 "
    "program that computes the single concrete calendar date meant by THIS "
    "specific reference date (treated as TODAY) and date phrase. Do NOT write a "
    "translation into the output. The phrase may be in English, French, "
    "Spanish, or a mixture of those languages; interpret its original meaning "
    "without discarding uncertainty or later corrections. If the date is "
    "tentative or a week-of-month convention is unspecified, print UNRESOLVED. "
    "Do NOT write a "
    "general-purpose parser that handles many phrasings - reason about the one "
    "given phrase yourself and write the minimal code that computes just that "
    "date. Use only the Python standard library (datetime, calendar). Define "
    "the reference date inline from the value given. The program MUST end by "
    "printing EXACTLY ONE line: the resolved date as YYYY-MM-DD, or the literal "
    "token " + _UNRESOLVED + " if it cannot be resolved to a single date. "
    "Never infer today's date from an invoice/due date or use the sandbox's "
    "wall clock. Use only the supplied reference date. A payment date must "
    "be on or after that date. If essential context is missing (for example "
    "an unspecified payday), or the requested calendar occurrence does not "
    "exist, print UNRESOLVED; do not invent a date. "
    "Output ONLY the complete Python source code - no markdown fences, no "
    "prose, no explanation, and make sure the program is syntactically "
    "complete.\n"
    "For an 'Nth <weekday> of <month>' phrase, use calendar.monthcalendar("
    "year, month), which returns weeks as lists where index 0=Monday..6=Sunday "
    "and 0 marks days outside the month; collect the non-zero entries for the "
    "target weekday column and take the Nth. Example - second Tuesday of a "
    "month:\n"
    "  import calendar, datetime\n"
    "  weeks = calendar.monthcalendar(year, month)\n"
    "  tuesdays = [w[calendar.TUESDAY] for w in weeks if w[calendar.TUESDAY]]\n"
    "  day = tuesdays[1]  # index 1 == second\n"
    "  print(datetime.date(year, month, day).isoformat())\n"
    "Adapt that pattern to the actual weekday and ordinal in the phrase."
)


def _code_gen_prompt(phrase: str, reference_date: str) -> str:
    """Build the user message supplying ONLY the arguments to the model.

    The orchestration layer provides the phrase + the reference ("today") date;
    the model writes the date math itself.
    """
    return (
        "Reference date (today): " + reference_date + "\n"
        "Natural-language date phrase: " + phrase + "\n\n"
        "Guidance: be careful and correct with the arithmetic; 'next month' is "
        "the month after the reference month (roll the year over in December). "
        "Verify the printed date actually falls on the requested weekday.\n\n"
        "Write the complete Python program now."
    )


def _converse_text(response: Mapping[str, Any]) -> str:
    """Pull the assistant text out of a Bedrock Converse response."""
    try:
        content = response["output"]["message"]["content"]
    except (KeyError, TypeError):
        return ""
    if not isinstance(content, list):
        return ""
    parts = [block["text"] for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)]
    return "".join(parts).strip()


def _strip_code_fences(text: str) -> str:
    """Remove ```python ... ``` fences a model may wrap the code in."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    # Drop the opening fence line (``` or ```python) and any trailing fence.
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _generate_program(
    phrase: str,
    reference_date: str,
    *,
    model_id: str,
    bedrock_client: Any,
    diagnostics: dict[str, Any] | None = None,
) -> str | None:
    """Ask the model to WRITE the resolution program; return its source.

    Returns ``None`` when the model returns nothing usable. Never raises.
    """
    try:
        response = bedrock_client.converse(
            modelId=model_id,
            system=[{"text": _CODE_GEN_SYSTEM}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": _code_gen_prompt(phrase, reference_date)}],
                }
            ],
            # Luna does not accept the temperature inference parameter.
            inferenceConfig={"maxTokens": 1500},
        )
    except Exception as exc:  # noqa: BLE001 - never raise into the caller
        record_diagnostic(diagnostics, "code_generation_failed", "code_generation", error_type=type(exc).__name__)
        logger.warning("code generation call failed (error_type=%s)", type(exc).__name__)
        return None
    if response.get("stopReason") not in (None, "end_turn", "stop_sequence"):
        record_diagnostic(diagnostics, "incomplete_model_program", "code_generation")
        return None
    code = _strip_code_fences(_converse_text(response))
    if not code:
        record_diagnostic(diagnostics, "empty_model_program", "code_generation")
    return code or None


def _wrap_generated_program(model_code: str) -> str:
    """Label model-generated source; output validation happens after execution."""
    return (
        "# --- model-generated date-resolution program (begin) ---\n"
        + model_code
        + "\n# --- model-generated date-resolution program (end) ---\n"
    )


def _default_client_factory(region: str) -> Any:
    """Build a real AgentCore Code Interpreter client (lazy import)."""
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

    return CodeInterpreter(region)


def _default_bedrock_client(region: str) -> Any:
    """Build a real bedrock-runtime client for code generation (lazy import)."""
    import boto3

    return boto3.session.Session(region_name=region).client("bedrock-runtime")


def _collect_text(response: Any) -> tuple[str, bool]:
    """Collect stdout text + isError flag from an executeCode stream response."""
    chunks: list[str] = []
    is_error = False
    for event in response.get("stream", []):
        result = event.get("result", {}) or {}
        if result.get("isError"):
            is_error = True
        for item in result.get("content", []) or []:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks), is_error


def resolve_date_phrase(
    phrase: str,
    reference_date: str | None = None,
    *,
    region: str,
    model_id: str | None = None,
    bedrock_client: Any | None = None,
    client_factory: Callable[[str], Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> str | None:
    """Resolve a natural-language date phrase to a concrete ISO date.

    A model WRITES a self-contained Python program from the supplied ``phrase``
    + ``reference_date`` (the orchestration layer only passes the arguments),
    and the AgentCore Code Interpreter sandbox EXECUTES it and prints the
    resolved ``YYYY-MM-DD``. This module supplies no date math of its own.

    Args:
        phrase: The raw natural-language date phrase (e.g. "next month, the
            first Monday", "in two weeks", "next Friday", "end of next month").
        reference_date: Optional ISO date (YYYY-MM-DD) treated as *today* by the
            model/program. Defaults to today (UTC).
        region: AWS region for the Code Interpreter session and bedrock client.
        model_id: TEXT model id used to GENERATE the program. Defaults to
            :data:`DEFAULT_CODE_GEN_MODEL_ID`.
        bedrock_client: Injectable ``bedrock-runtime`` client whose ``converse``
            returns the generated program. Stubbed in tests so no AWS call
            happens. Defaults to the real client built from boto3.
        client_factory: Optional factory ``(region) -> client`` returning an
            object with ``start()``, ``invoke(method, params)`` and ``stop()``.
            Injected in tests so no AWS call happens. Defaults to the real
            AgentCore client.

    Returns:
        The resolved ISO date string, or ``None`` when the phrase cannot be
        resolved or the model/sandbox errors. Never raises into the caller.
        Optional ``diagnostics`` records the actual branch without program,
        stdout, or exception payloads.
    """
    text = (phrase or "").strip()
    if not text:
        record_diagnostic(diagnostics, "empty_phrase", "input")
        return None

    issues = phrase_clarification_reasons(text)
    if issues:
        record_diagnostic(diagnostics, issues[0], "clarification", phrase_issues=issues)
        return None

    if reference_date and reference_date.strip():
        ref = reference_date.strip()
    else:
        ref = datetime.now(timezone.utc).date().isoformat()
    try:
        datetime.strptime(ref, "%Y-%m-%d")
    except ValueError:
        record_diagnostic(diagnostics, "invalid_reference_date", "input")
        return None

    # Exact ISO input still obeys the same calendar/future guard as generated
    # output, so a plausible-looking past date cannot bypass validation.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        record_diagnostic(diagnostics, payment_date_rejection(text, ref), "input")
        return validate_payment_date(text, ref)

    # Step 1: have the MODEL author the resolution program.
    record_diagnostic(diagnostics, None, "code_generation", fallback_attempted=True)
    try:
        model = bedrock_client or _default_bedrock_client(region)
    except Exception as exc:  # noqa: BLE001 - unavailable SDK configuration
        record_diagnostic(diagnostics, "code_generation_failed", "code_generation", error_type=type(exc).__name__)
        logger.warning("code generation client unavailable (error_type=%s)", type(exc).__name__)
        return None
    record_diagnostic(diagnostics, None, "code_generation", code_generation_attempted=True)
    model_code = _generate_program(
        text, ref, model_id=model_id or DEFAULT_CODE_GEN_MODEL_ID, bedrock_client=model,
        diagnostics=diagnostics,
    )
    if not model_code:
        logger.info("date resolution model returned no usable program")
        return None
    program = _wrap_generated_program(model_code)

    # Step 2: EXECUTE the model's program in the AgentCore sandbox.
    factory = client_factory or _default_client_factory
    client = None
    try:
        record_diagnostic(diagnostics, None, "code_interpreter", sandbox_attempted=True)
        client = factory(region)
        client.start()
        response = client.invoke(
            "executeCode", {"language": "python", "code": program}
        )
        stdout, is_error = _collect_text(response)
        if is_error:
            record_diagnostic(diagnostics, "code_interpreter_execution_error", "code_interpreter")
            logger.warning("date resolution code interpreter returned isError")
            return None
        output = (stdout or "").strip()
        if output == _UNRESOLVED:
            record_diagnostic(diagnostics, "model_unresolved", "output_validation")
            return None
        match = _ISO_DATE_RE.fullmatch(output)
        if not match:
            record_diagnostic(diagnostics, "invalid_model_output", "output_validation")
            logger.info("date resolution code interpreter produced invalid output")
            return None
        iso = validate_payment_date(match.group(1), ref)
        if iso is None:
            record_diagnostic(diagnostics, payment_date_rejection(match.group(1), ref), "output_validation")
            return None
        record_diagnostic(diagnostics, None, "output_validation")
        logger.info(
            "model-authored program resolved a valid payment date",
            extra={"event_type": "code_interpreter", "tool": "resolve_date_phrase"},
        )
        return iso
    except Exception as exc:  # noqa: BLE001 - never raise into the caller
        record_diagnostic(diagnostics, "code_interpreter_failed", "code_interpreter", error_type=type(exc).__name__)
        logger.warning(
            "code interpreter failed (error_type=%s)", type(exc).__name__
        )
        return None
    finally:
        if client is not None:
            try:
                client.stop()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                if diagnostics is not None:
                    diagnostics["cleanup_error_type"] = type(exc).__name__
                logger.warning("code interpreter stop() failed (error_type=%s)", type(exc).__name__)
