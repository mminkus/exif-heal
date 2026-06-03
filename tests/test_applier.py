"""Tests for apply-time provenance filtering."""

from exif_heal.applier import _filter_provenance

FULL = {
    "time_source": "neighbor_interp", "time_confidence": "high",
    "gps_source": "default_hint", "gps_confidence": "low",
}


class TestFilterProvenance:

    def test_both_written_keeps_all(self):
        assert _filter_provenance(FULL, wrote_time=True, wrote_gps=True) == FULL

    def test_time_only_drops_gps_provenance(self):
        # Codex repro: high-conf time + gated LOW GPS -> must NOT stamp GPSSource.
        out = _filter_provenance(FULL, wrote_time=True, wrote_gps=False)
        assert out == {"time_source": "neighbor_interp", "time_confidence": "high"}
        assert "gps_source" not in out and "gps_confidence" not in out

    def test_gps_only_drops_time_provenance(self):
        out = _filter_provenance(FULL, wrote_time=False, wrote_gps=True)
        assert out == {"gps_source": "default_hint", "gps_confidence": "low"}

    def test_nothing_written_returns_none(self):
        assert _filter_provenance(FULL, wrote_time=False, wrote_gps=False) is None
