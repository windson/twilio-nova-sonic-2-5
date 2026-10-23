"""Fail loudly if the tested GA Strands / async Bedrock SDK APIs disappear."""

def test_strands_bidi_imports():
    from strands.bidi.agent import BidiAgent
    from strands.bidi.models import BedrockNovaSonicModel
    from aws_sdk_bedrock_runtime.client import AsyncBedrockRuntimeClient
    from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
    assert BidiAgent and BedrockNovaSonicModel and AsyncBedrockRuntimeClient and AsyncBedrockRuntimeConfig
