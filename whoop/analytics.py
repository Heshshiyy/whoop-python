"""Analytics engine for WHOOP data — HRV, recovery, strain, sleep.

Zero external dependencies — uses only stdlib math/statistics.
"""

from __future__ import annotations

import math
import statistics
from typing import Sequence


# ---------------------------------------------------------------------------
# Ectopic / artifact filter (Malik 1996)
# ---------------------------------------------------------------------------

def malik_ectopic_filter(rr_intervals: Sequence[float]) -> list[float]:
    """Remove ectopic beats using the Malik criterion.

    An RR interval is retained only if it lies within 20 % of the mean of the
    preceding five normal intervals (fallback to two on boundaries).

    Reference:
        Malik, M. et al. (1996). Heart rate variability: Standards of
        measurement, physiological interpretation, and clinical use.
        *European Heart Journal*, 17(3), 354-381.
    """
    if len(rr_intervals) < 3:
        return list(rr_intervals)

    rr = list(rr_intervals)
    clean: list[float] = []

    for i, val in enumerate(rr):
        # Build reference window
        if i == 0:
            refs = rr[1:3]  # next 2
        elif i == 1:
            refs = [rr[0]] + rr[2:3]
        else:
            refs = rr[max(0, i - 5) : i]

        if not refs:
            clean.append(val)
            continue

        mean_ref = statistics.mean(refs)
        if 0.8 * mean_ref <= val <= 1.2 * mean_ref:
            clean.append(val)

    return clean


# ---------------------------------------------------------------------------
# HRV metrics
# ---------------------------------------------------------------------------

def compute_rmssd(rr_intervals: Sequence[float]) -> float:
    """Root Mean Square of Successive Differences (RMSSD).

    RMSSD = sqrt(mean((RR_i - RR_{i+1})^2))

    A primary time-domain HRV measure reflecting parasympathetic activity.
    """
    if len(rr_intervals) < 2:
        return 0.0
    diffs = [
        (rr_intervals[i + 1] - rr_intervals[i]) ** 2
        for i in range(len(rr_intervals) - 1)
    ]
    return math.sqrt(statistics.mean(diffs))


def compute_sdnn(rr_intervals: Sequence[float]) -> float:
    """Standard Deviation of NN (normal-to-normal) intervals.

    Reflects total HRV including both sympathetic and parasympathetic
    contributions.
    """
    if len(rr_intervals) < 2:
        return 0.0
    return statistics.stdev(rr_intervals)


def compute_pnn50(rr_intervals: Sequence[float]) -> float:
    """Percentage of successive NN intervals differing by > 50 ms.

    Sensitive to short-term, high-frequency HRV.
    """
    if len(rr_intervals) < 2:
        return 0.0
    count = sum(
        1 for i in range(len(rr_intervals) - 1)
        if abs(rr_intervals[i + 1] - rr_intervals[i]) > 50
    )
    return (count / (len(rr_intervals) - 1)) * 100.0


# ---------------------------------------------------------------------------
# WHOOP-style recovery (0-100)
# ---------------------------------------------------------------------------

# Population norms (approximate — WHOOP uses personal baselines)
_DEFAULT_HRV_NORM = 50.0       # ms, typical RMSSD
_DEFAULT_RESTING_HR_NORM = 60.0
_DEFAULT_SLEEP_EFF_NORM = 90.0  # %


def compute_recovery(
    hrv_rmssd: float,
    resting_hr: float,
    sleep_efficiency: float,
    strain: float,
) -> float:
    """Compute a WHOOP-style recovery score (0-100).

    Weighted combination of HRV, RHR, sleep efficiency, and prior-day strain.

    - Higher HRV (relative to baseline) → higher recovery.
    - Lower resting HR → higher recovery.
    - Higher sleep efficiency → higher recovery.
    - Lower prior strain → higher recovery.
    """
    # HRV component (normalised: higher is better)
    hrv_score = min(100.0, max(0.0, (hrv_rmssd / _DEFAULT_HRV_NORM) * 50.0))
    if hrv_rmssd <= 0:
        hrv_score = 25.0

    # RHR component (normalised: lower is better)
    rhr_score = min(100.0, max(0.0, (1.0 - (resting_hr - 40) / 60.0) * 25.0))
    if resting_hr <= 0:
        rhr_score = 12.5

    # Sleep component
    sleep_score = min(100.0, max(0.0, (sleep_efficiency / _DEFAULT_SLEEP_EFF_NORM) * 15.0))

    # Strain penalty (0-21 scale → 0-10 penalty)
    strain_penalty = min(10.0, strain * 0.476)  # 21 → 10

    recovery = hrv_score + rhr_score + sleep_score - strain_penalty
    return round(max(0.0, min(100.0, recovery)), 1)


# ---------------------------------------------------------------------------
# WHOOP-style cardiovascular strain (0-21)
# ---------------------------------------------------------------------------

def compute_strain(
    hr_samples: Sequence[int],
    resting_hr: int,
    max_hr: int,
) -> float:
    """Compute a WHOOP-style strain score (0-21).

    Based on time spent in heart-rate zones using a TRIMP-style
    (TRaining IMPulse) approach.

    Zones:
      - Zone 1: 50-60 % HRR → weight 1
      - Zone 2: 60-70 % HRR → weight 2
      - Zone 3: 70-80 % HRR → weight 3
      - Zone 4: 80-90 % HRR → weight 5
      - Zone 5: >90 % HRR  → weight 8
    """
    if not hr_samples or max_hr <= resting_hr:
        return 0.0

    hrr = max_hr - resting_hr  # heart rate reserve
    zone_counts = [0, 0, 0, 0, 0]  # Z1-Z5
    zone_weights = [1, 2, 3, 5, 8]

    for hr in hr_samples:
        pct = (hr - resting_hr) / hrr if hrr > 0 else 0
        if pct <= 0.50:
            continue  # below zone 1
        elif pct <= 0.60:
            zone_counts[0] += 1
        elif pct <= 0.70:
            zone_counts[1] += 1
        elif pct <= 0.80:
            zone_counts[2] += 1
        elif pct <= 0.90:
            zone_counts[3] += 1
        else:
            zone_counts[4] += 1

    # TRIMP-like accumulation
    trimp = sum(zc * zw for zc, zw in zip(zone_counts, zone_weights))
    # Normalise to 0-21 scale (log scaling, like WHOOP)
    if trimp <= 0:
        return 0.0
    strain = 8.0 * math.log1p(trimp / 15.0)
    return round(max(0.0, min(21.0, strain)), 1)


# ---------------------------------------------------------------------------
# Resting HR estimation
# ---------------------------------------------------------------------------

def compute_resting_hr(hr_samples: Sequence[int]) -> int:
    """Estimate resting heart rate as the lowest sustained HR.

    Uses the 5th percentile of HR values as a robust estimate,
    or the minimum if fewer than 20 samples.
    """
    if not hr_samples:
        return 60
    if len(hr_samples) < 20:
        return min(hr_samples)
    sorted_hr = sorted(hr_samples)
    idx = max(0, int(len(sorted_hr) * 0.05))
    return sorted_hr[idx]


# ---------------------------------------------------------------------------
# Sleep stage detection (simplified)
# ---------------------------------------------------------------------------

def detect_sleep_stages(
    hr_samples: Sequence[int],
    motion_samples: Sequence[float] | None = None,
) -> dict[str, float]:
    """Detect approximate sleep stages from HR and optional motion data.

    Returns a dict with estimated minutes in each stage:
    { "awake": float, "light": float, "deep": float, "rem": float }

    This is a simplified heuristic — real WHOOP uses PPG + accelerometer
    machine-learning models.
    """
    if not hr_samples:
        return {"awake": 0.0, "light": 0.0, "deep": 0.0, "rem": 0.0}

    resting = compute_resting_hr(hr_samples)
    total_epochs = len(hr_samples)
    # Assume each sample is ~60 seconds
    minutes_per_epoch = 1.0

    awake = light = deep = rem = 0.0

    for i, hr in enumerate(hr_samples):
        motion = motion_samples[i] if motion_samples and i < len(motion_samples) else 0.0

        if motion > 0.5:
            awake += minutes_per_epoch
        elif hr > resting * 1.15:
            # Elevated HR → REM or awake
            if motion > 0.2:
                awake += minutes_per_epoch
            else:
                rem += minutes_per_epoch
        elif hr < resting * 0.90:
            deep += minutes_per_epoch
        else:
            light += minutes_per_epoch

    return {
        "awake": round(awake, 1),
        "light": round(light, 1),
        "deep": round(deep, 1),
        "rem": round(rem, 1),
    }
