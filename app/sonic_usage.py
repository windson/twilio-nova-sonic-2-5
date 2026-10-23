"""Accumulate the pinned Strands provider's disjoint, incremental token counts.

No pricing, I/O, tokenization, or model invocation belongs on this path. The
same accumulator reconstructs old calls from their recorded usage events.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        return int(number) if number.is_finite() and number >= 0 and number == int(number) else None
    except (ValueError, OverflowError):
        return None


class UsageAccumulator:
    """Each add is one BidiUsageEvent, never a provider cumulative snapshot.

    ``complete`` describes coverage of the observed events. It does not claim
    AWS invoice reconciliation or receipt of usage lost in a network failure.
    Missing detail cannot silently become a zero-priced modality.
    """

    def __init__(self) -> None:
        self.event_count = 0
        self.invalid_event_count = 0
        self.totals = {key: 0 for key in ("input_tokens", "output_tokens", "total_tokens")}
        self.details: dict[str, dict[str, int]] = {"input": {}, "output": {}}
        self.cache: dict[str, int | None] = {
            "cache_read_input_tokens": None, "cache_write_input_tokens": None,
        }

    def add(self, delta: Mapping[str, Any]) -> None:
        self.event_count += 1
        counts = {key: _count(delta.get(key)) for key in self.totals}
        valid = all(value is not None for value in counts.values())
        if valid:
            valid = counts["input_tokens"] + counts["output_tokens"] == counts["total_tokens"]
        for key, value in counts.items():
            if value is not None:
                self.totals[key] += value
        modalities = delta.get("modality_details")
        modalities = modalities if isinstance(modalities, Mapping) else {}
        for direction in ("input", "output"):
            detail = modalities.get(direction)
            detail = detail if isinstance(detail, Mapping) else {}
            parsed = {key: _count(value) for key, value in detail.items()}
            # A reported zero total establishes zero text/audio even when an
            # optional detail map is omitted. Positive totals require details.
            if counts[f"{direction}_tokens"] == 0:
                parsed.setdefault("text", 0)
                parsed.setdefault("audio", 0)
            pair = [parsed.get(key) for key in ("text", "audio")]
            if any(value is None for value in pair) or sum(value or 0 for value in pair) != counts[f"{direction}_tokens"]:
                valid = False
            for key, value in parsed.items():
                if value is None:
                    valid = False
                else:
                    self.details[direction][key] = self.details[direction].get(key, 0) + value
        for key in self.cache:
            value = delta.get(key)
            if value is not None:
                count = _count(value)
                if count is None:
                    valid = False
                else:
                    self.cache[key] = (self.cache[key] or 0) + count
        if not valid:
            self.invalid_event_count += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.totals, **self.cache,
            "modality_details": {key: dict(value) for key, value in self.details.items()},
            "accounting": "cumulative_deltas",
            "event_count": self.event_count,
            "invalid_event_count": self.invalid_event_count,
            "complete": self.event_count > 0 and self.invalid_event_count == 0,
        }
