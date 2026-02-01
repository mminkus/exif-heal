"""Confidence scoring, guardrails, and gating logic."""

from __future__ import annotations

from .models import Confidence, ProposedChange


def apply_confidence_gate(
    change: ProposedChange,
    min_confidence_time: Confidence = Confidence.MED,
    min_confidence_gps: Confidence = Confidence.MED,
) -> ProposedChange:
    """Apply confidence gating to a proposed change.

    Changes below the minimum confidence are flagged as gated
    but kept in the report for visibility.
    """
    reasons = []

    if change.has_time_change and change.time_confidence < min_confidence_time:
        change.gated_time = True
        reasons.append(
            f"time confidence {change.time_confidence.value} "
            f"< threshold {min_confidence_time.value}"
        )

    if change.has_gps_change and change.gps_confidence < min_confidence_gps:
        change.gated_gps = True
        reasons.append(
            f"GPS confidence {change.gps_confidence.value} "
            f"< threshold {min_confidence_gps.value}"
        )

    if reasons:
        change.gate_reason = "; ".join(reasons)

    return change


def parse_confidence(value: str) -> Confidence:
    """Parse a confidence level from a CLI string."""
    try:
        return Confidence(value.lower())
    except ValueError:
        valid = ", ".join(c.value for c in Confidence if c != Confidence.NONE)
        raise ValueError(f"Invalid confidence level '{value}'. Valid: {valid}")
