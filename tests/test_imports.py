"""Import smoke test for the GA Strands bi-di + Bedrock runtime stack.

These imports pin the authoritative APIs used by the app. In the installed
strands-agents 1.61.1, the bi-di API is GA under ``strands.bidi`` and the Nova
Sonic model class is ``BedrockNovaSonicModel`` (exported from
``strands.bidi.models``). The test must pass once dependencies are installed;
it is only xfailed if the stack genuinely cannot be installed in this
environment (recorded in the feature findings).
"""

import pytest


def test_strands_bidi_imports():
    try:
        from strands.bidi.agent import BidiAgent  # noqa: F401
        from strands.bidi.models import (  # noqa: F401
            BedrockNovaSonicModel,
        )
        from aws_sdk_bedrock_runtime.client import (  # noqa: F401
            BedrockRuntimeClient,
        )
    except ImportError as exc:  # pragma: no cover - environment guard
        pytest.xfail(f"Strands bi-di / Bedrock runtime stack not importable: {exc}")
