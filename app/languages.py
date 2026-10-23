"""Application language policy for Nova's existing polyglot Tiffany voice.

Nova detects and switches languages natively. This allowlist guides conversation
and validates closing-tool arguments; it does not disable model recognition of
other languages or create a separate translation/speech pipeline.
"""

from typing import Literal

ConversationLanguage = Literal["en", "fr", "es"]
SUPPORTED_CONVERSATION_LANGUAGES = ("en", "fr", "es")
LANGUAGE_NAMES = {"en": "English", "fr": "French", "es": "Spanish"}
POLYGLOT_VOICE = "tiffany"


def closing_for_language(
    language: str, *, english_greeting: str, english_phrase: str,
) -> tuple[str, str]:
    """Return one fixed farewell and its required transcript phrase.

    English keeps the existing configured greeting and phrase. Never fall back
    to English when an unsupported language was explicitly requested.
    """
    if language == "en":
        return english_greeting, english_phrase
    if language == "fr":
        return "Merci pour votre temps. Au revoir.", "Au revoir"
    if language == "es":
        return "Gracias por su tiempo. Adiós.", "Adiós"
    raise ValueError("Supported conversation languages are en, fr, and es")
