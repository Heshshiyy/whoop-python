"""Tests for the WHOOP analytics engine.

Covers RMSSD, SDNN, pNN50, ectopic filter, recovery, strain, resting HR,
and sleep-stage detection.
"""

import math
import pytest

from whoop.analytics import (
    compute_rmssd,
    compute_sdnn,
    compute_pnn50,
    malik_ectopic_filter,
    compute_recovery,
    compute_strain,
    compute_resting_hr,
    detect_sleep_stages,
)


# ---------------------------------------------------------------------------
# RMSSD
# ---------------------------------------------------------------------------

class TestRmssd:
    def test_known_vector(self):
        """RMSSD of [800, 820, 810, 830] ms.
        Diffs: 20, 10, 20 → squares: 400, 100, 400 → mean: 300 → sqrt: 17.32
        """
        rr = [800.0, 820.0, 810.0, 830.0]
        result = compute_rmssd(rr)
        assert math.isclose(result, math.sqrt(300), rel_tol=1e-6)

    def test_two_points(self):
        """Single diff: [1000, 1050] → diff=50 → rmssd=50."""
        assert compute_rmssd([1000.0, 1050.0]) == 50.0

    def test_identical_values(self):
        assert compute_rmssd([800.0, 800.0, 800.0]) == 0.0

    def test_empty_returns_zero(self):
        assert compute_rmssd([]) == 0.0

    def test_single_value_returns_zero(self):
        assert compute_rmssd([800.0]) == 0.0


# ---------------------------------------------------------------------------
# SDNN
# ---------------------------------------------------------------------------

class TestSdnn:
    def test_known_vector(self):
        """SDNN of [800, 810, 820] → stdev ≈ 10."""
        assert math.isclose(compute_sdnn([800.0, 810.0, 820.0]), 10.0, rel_tol=1e-6)

    def test_identical(self):
        assert compute_sdnn([800.0, 800.0, 800.0]) == 0.0

    def test_short_returns_zero(self):
        assert compute_sdnn([]) == 0.0
        assert compute_sdnn([800.0]) == 0.0


# ---------------------------------------------------------------------------
# pNN50
# ---------------------------------------------------------------------------

class TestPnn50:
    def test_no_big_diffs(self):
        """All diffs ≤ 50 → 0 %."""
        assert compute_pnn50([800.0, 820.0, 840.0]) == 0.0

    def test_all_big_diffs(self):
        """Diffs of 60 → 100 %."""
        assert compute_pnn50([800.0, 860.0, 800.0, 860.0]) == 100.0

    def test_half_big_diffs(self):
        """2 diffs: 55 (>50) and 30 (≤50) → 50 %."""
        assert compute_pnn50([800.0, 855.0, 885.0]) == 50.0

    def test_short_returns_zero(self):
        assert compute_pnn50([]) == 0.0
        assert compute_pnn50([800.0]) == 0.0


# ---------------------------------------------------------------------------
# Ectopic filter
# ---------------------------------------------------------------------------

class TestEctopicFilter:
    def test_no_removal_on_normal(self):
        """Normal RR values should pass through."""
        rr = [800.0, 810.0, 820.0, 790.0, 805.0]
        result = malik_ectopic_filter(rr)
        assert len(result) == 5

    def test_removes_spike(self):
        """A large spike should be removed."""
        rr = [800.0, 800.0, 800.0, 800.0, 800.0, 2000.0]  # last is > 1.2 * mean
        result = malik_ectopic_filter(rr)
        assert 2000.0 not in result

    def test_short_sequence_passes(self):
        assert malik_ectopic_filter([800.0]) == [800.0]
        assert malik_ectopic_filter([800.0, 820.0]) == [800.0, 820.0]


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

class TestRecovery:
    def test_perfect_recovery(self):
        """High HRV, low RHR, perfect sleep, zero strain → 80-100."""
        score = compute_recovery(
            hrv_rmssd=80.0,
            resting_hr=45.0,
            sleep_efficiency=100.0,
            strain=0.0,
        )
        assert 80.0 <= score <= 100.0

    def test_poor_recovery(self):
        """Low HRV, high RHR, poor sleep, high strain → low."""
        score = compute_recovery(
            hrv_rmssd=20.0,
            resting_hr=75.0,
            sleep_efficiency=50.0,
            strain=20.0,
        )
        assert score < 50.0

    def test_bounds(self):
        """Recovery must be in 0-100 range."""
        score = compute_recovery(hrv_rmssd=0, resting_hr=200,
                                 sleep_efficiency=0, strain=21)
        assert 0.0 <= score <= 100.0

        score = compute_recovery(hrv_rmssd=999, resting_hr=30,
                                 sleep_efficiency=100, strain=0)
        assert 0.0 <= score <= 100.0

    def test_zero_inputs(self):
        score = compute_recovery(hrv_rmssd=0, resting_hr=0,
                                 sleep_efficiency=0, strain=0)
        assert 0.0 <= score <= 100.0


# ---------------------------------------------------------------------------
# Strain
# ---------------------------------------------------------------------------

class TestStrain:
    def test_no_activity(self):
        """All HR at resting level → zero strain."""
        strain = compute_strain([60, 60, 60, 60], resting_hr=60, max_hr=190)
        assert strain == 0.0

    def test_max_effort(self):
        """All HR at max → moderate-high strain (scales with sample count)."""
        strain_few = compute_strain([190, 190, 190], resting_hr=60, max_hr=190)
        strain_many = compute_strain([190] * 120, resting_hr=60, max_hr=190)
        assert 5.0 <= strain_few <= 21.0
        assert 12.0 <= strain_many <= 21.0

    def test_moderate_effort(self):
        """HR around 75% HRR → moderate strain."""
        hrr = 190 - 60  # 130
        hr75 = 60 + int(0.75 * hrr)  # ~157
        strain = compute_strain([hr75] * 30, resting_hr=60, max_hr=190)
        assert 10.0 <= strain <= 18.0

    def test_bounds(self):
        """Strain must be in 0-21 range."""
        strain = compute_strain([200] * 1000, resting_hr=60, max_hr=190)
        assert 0.0 <= strain <= 21.0

    def test_empty_samples(self):
        assert compute_strain([], resting_hr=60, max_hr=190) == 0.0

    def test_invalid_params(self):
        """max_hr <= resting_hr → zero."""
        assert compute_strain([100, 110], resting_hr=100, max_hr=100) == 0.0


# ---------------------------------------------------------------------------
# Resting HR
# ---------------------------------------------------------------------------

class TestRestingHr:
    def test_minimum_for_few_samples(self):
        assert compute_resting_hr([72, 68, 75]) == 68

    def test_percentile_for_many(self):
        """5th percentile of 100 values."""
        samples = list(range(50, 150))
        result = compute_resting_hr(samples)
        # 5th percentile of [50..149] → index 5 → 55
        assert result == 55

    def test_empty(self):
        assert compute_resting_hr([]) == 60


# ---------------------------------------------------------------------------
# Sleep stages
# ---------------------------------------------------------------------------

class TestSleepStages:
    def test_returns_all_stages(self):
        stages = detect_sleep_stages([60, 62, 58, 65, 60])
        assert "awake" in stages
        assert "light" in stages
        assert "deep" in stages
        assert "rem" in stages
        total = sum(stages.values())
        assert total == pytest.approx(5.0, rel=1e-6)

    def test_empty_input(self):
        stages = detect_sleep_stages([])
        assert stages == {"awake": 0.0, "light": 0.0, "deep": 0.0, "rem": 0.0}

    def test_motion_causes_awake(self):
        """High motion samples → awake classification."""
        stages = detect_sleep_stages([60] * 10, motion_samples=[1.0] * 10)
        assert stages["awake"] > stages["deep"]
        assert stages["awake"] > stages["rem"]
