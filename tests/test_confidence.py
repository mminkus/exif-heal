"""Tests for confidence scoring, guardrails, and gating logic."""

from pathlib import Path

import pytest

from exif_heal.confidence import apply_confidence_gate, parse_confidence
from exif_heal.models import Confidence, GPSCoord, GPSSource, ProposedChange, TimeSource


class TestConfidenceComparison:

    def test_ordering(self):
        assert Confidence.HIGH > Confidence.MED
        assert Confidence.MED > Confidence.LOW
        assert Confidence.LOW > Confidence.NONE
        assert Confidence.HIGH >= Confidence.HIGH
        assert Confidence.MED >= Confidence.LOW

    def test_not_greater(self):
        assert not (Confidence.LOW > Confidence.MED)
        assert not (Confidence.NONE > Confidence.LOW)


class TestConfidenceGating:

    def _make_change(
        self,
        time_conf: Confidence = Confidence.NONE,
        gps_conf: Confidence = Confidence.NONE,
        has_time: bool = False,
        has_gps: bool = False,
    ) -> ProposedChange:
        change = ProposedChange(path=Path("/test/photo.jpg"))
        if has_time:
            change.new_datetime_original = "2020:01:01 10:00:00"
            change.new_create_date = "2020:01:01 10:00:00"
            change.time_confidence = time_conf
            change.time_source = TimeSource.NEIGHBOR_INTERP
        if has_gps:
            change.new_gps = GPSCoord(-34.5, 138.5)
            change.gps_confidence = gps_conf
            change.gps_source = GPSSource.NEIGHBOR_COPY
        return change

    def test_high_confidence_passes(self):
        change = self._make_change(
            time_conf=Confidence.HIGH, has_time=True,
        )
        apply_confidence_gate(change, Confidence.MED, Confidence.MED)
        assert change.gated_time is False

    def test_med_confidence_passes_med_threshold(self):
        change = self._make_change(
            time_conf=Confidence.MED, has_time=True,
        )
        apply_confidence_gate(change, Confidence.MED, Confidence.MED)
        assert change.gated_time is False

    def test_low_confidence_gated_by_med_threshold(self):
        change = self._make_change(
            time_conf=Confidence.LOW, has_time=True,
        )
        apply_confidence_gate(change, Confidence.MED, Confidence.MED)
        assert change.gated_time is True

    def test_low_confidence_passes_low_threshold(self):
        change = self._make_change(
            time_conf=Confidence.LOW, has_time=True,
        )
        apply_confidence_gate(change, Confidence.LOW, Confidence.MED)
        assert change.gated_time is False

    def test_gps_gated_independently(self):
        change = self._make_change(
            time_conf=Confidence.HIGH, has_time=True,
            gps_conf=Confidence.LOW, has_gps=True,
        )
        apply_confidence_gate(change, Confidence.MED, Confidence.MED)
        assert change.gated_time is False
        assert change.gated_gps is True

    def test_default_gps_hint_gated(self):
        change = self._make_change(gps_conf=Confidence.LOW, has_gps=True)
        change.gps_source = GPSSource.DEFAULT_HINT
        apply_confidence_gate(change, Confidence.MED, Confidence.MED)
        assert change.gated_gps is True

    def test_gate_reason_set(self):
        change = self._make_change(
            time_conf=Confidence.LOW, has_time=True,
            gps_conf=Confidence.LOW, has_gps=True,
        )
        apply_confidence_gate(change, Confidence.MED, Confidence.MED)
        assert "time confidence" in change.gate_reason
        assert "GPS confidence" in change.gate_reason


class TestParseConfidence:

    def test_valid_values(self):
        assert parse_confidence("high") == Confidence.HIGH
        assert parse_confidence("med") == Confidence.MED
        assert parse_confidence("low") == Confidence.LOW

    def test_case_insensitive(self):
        assert parse_confidence("HIGH") == Confidence.HIGH
        assert parse_confidence("Med") == Confidence.MED

    def test_invalid_value(self):
        with pytest.raises(ValueError, match="Invalid confidence"):
            parse_confidence("ultra")
