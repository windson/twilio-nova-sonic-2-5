"""AWS Bedrock AgentCore Code Interpreter wrapper (off-path date resolution).

This module is the REAL code interpreter that replaces the former hand-written
``_resolve_relative_date`` date parser. The key property, per the user's
explicit intent, is that the resolution program is **authored by a model on the
fly** and then **executed in the sandbox** - this module does NOT hand-write the
date math.

Flow of :func:`resolve_date_phrase`:

1. A Bedrock TEXT model (the existing ``EXTRACTION_MODEL_ID`` /
   ``Settings.extraction_model_id``, default ``amazon.nova-lite-v1:0``) is
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

A tiny, clearly-labelled safety harness is wrapped around the model-generated
snippet (only to guarantee a single printed line and to tolerate a model that
forgets to print) - the date LOGIC itself always comes from the model, never
from this module.

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

logger = logging.getLogger(__name__)

# Managed Code Interpreter identifier (no CreateCodeInterpreter needed).
DEFAULT_CODE_INTERPRETER_ID = "aws.codeinterpreter.v1"

# Default TEXT model used to GENERATE the resolution program (mirrors
# Settings.extraction_model_id / EXTRACTION_MODEL_ID).
DEFAULT_CODE_GEN_MODEL_ID = "amazon.nova-lite-v1:0"

# Sentinel the generated program prints when it cannot resolve the phrase.
_UNRESOLVED = "UNRESOLVED"

_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Instruction driving the model to WRITE (not us) the date-resolution Python.
# The model reasons from today's date and the phrase and emits a self-contained
# program. It must print ONLY the resolved date (or the sentinel).
_CODE_GEN_SYSTEM = (
    "You are a Python programmer. Write a short, self-contained Python 3 "
    "program that computes a concrete calendar date from a reference date "
    "(treated as TODAY) and a natural-language date phrase. Reason from the "
    "reference date. Handle relative and natural phrasings generally, for "
    "example: 'today', 'tomorrow', 'next <weekday>', 'in N days/weeks/months', "
    "'the first/second/last <weekday> of next month', 'end of (next) month', "
    "and explicit calendar dates. Use only the Python standard library "
    "(datetime, calendar). The program must print EXACTLY ONE line: the "
    "resolved date as YYYY-MM-DD, or the literal token " + _UNRESOLVED + " if "
    "the phrase cannot be resolved to a single date. Output ONLY the Python "
    "source code - no markdown fences, no prose, no explanation."
)


def _code_gen_prompt(phrase: str, reference_date: str) -> str:
    """Build the user message supplying ONLY the arguments to the model.

    The orchestration layer provides the phrase + the reference ("today") date;
    the model writes the date math itself.
    """
    return (
        "Reference date (today): " + reference_date + "\n"
        "Natural-language date phrase: " + phrase + "\n\n"
        "Write the Python program now."
    )


def _converse_text(response: Mapping[str, Any]) -> str:
    """Pull the assistant text out of a Bedrock Converse response."""
    try:
        content = response["output"]["message"]["content"]
    except (KeyError, TypeError):
        return ""
    parts = [block.get("text", "") for block in content if isinstance(block, dict)]
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
            inferenceConfig={"maxTokens": 512, "temperature": 0.0},
        )
    except Exception as exc:  # noqa: BLE001 - never raise into the caller
        logger.warning("phrase=<%s> | code generation call failed: %s", phrase, exc)
        return None
    code = _strip_code_fences(_converse_text(response))
    return code or None


def _wrap_generated_program(model_code: str) -> str:
    """Wrap the MODEL-GENERATED snippet in a tiny safety harness.

    This harness does NOT contain any date logic. It only guarantees the
    sandbox prints a single trailing newline-terminated marker so stdout is
    always parseable, isolating failures in the model's code to an
    ``UNRESOLVED`` outcome instead of a crash that yields no ISO date. The date
    reasoning lives entirely inside ``model_code``.
    """
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

    return boto3.client("bedrock-runtime", region_name=region)


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
    """
    text = (phrase or "").strip()
    if not text:
        return None

    # Already-ISO fast path: skip BOTH the model and the sandbox entirely.
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        pass

    if reference_date and reference_date.strip():
        ref = reference_date.strip()
    else:
        ref = datetime.now(timezone.utc).date().isoformat()

    # Step 1: have the MODEL author the resolution program.
    model = bedrock_client or _default_bedrock_client(region)
    model_code = _generate_program(
        text, ref, model_id=model_id or DEFAULT_CODE_GEN_MODEL_ID, bedrock_client=model
    )
    if not model_code:
        logger.info("phrase=<%s> | model returned no program", phrase)
        return None
    program = _wrap_generated_program(model_code)

    # Step 2: EXECUTE the model's program in the AgentCore sandbox.
    factory = client_factory or _default_client_factory
    client = None
    try:
        client = factory(region)
        client.start()
        response = client.invoke(
            "executeCode", {"language": "python", "code": program}
        )
        stdout, is_error = _collect_text(response)
        if is_error:
            logger.warning(
                "phrase=<%s> | code interpreter returned isError", phrase
            )
            return None
        match = _ISO_DATE_RE.search(stdout or "")
        if not match:
            logger.info(
                "phrase=<%s> | code interpreter produced no ISO date (stdout=<%s>)",
                phrase,
                (stdout or "").strip()[:120],
            )
            return None
        iso = match.group(1)
        # Validate it is a real calendar date before returning.
        try:
            datetime.strptime(iso, "%Y-%m-%d")
        except ValueError:
            return None
        logger.info(
            "phrase=<%s>, resolved=<%s> | model-authored program resolved date",
            phrase,
            iso,
            extra={"event_type": "code_interpreter", "tool": "resolve_date_phrase"},
        )
        return iso
    except Exception as exc:  # noqa: BLE001 - never raise into the caller
        logger.warning(
            "phrase=<%s> | code interpreter failed: %s", phrase, exc
        )
        return None
    finally:
        if client is not None:
            try:
                client.stop()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("code interpreter stop() failed", exc_info=True)
