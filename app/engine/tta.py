"""Time-to-turbulence (TTA), its conservative lower bound Q_low, and the crew point stage.

P1 / P2 / P3 start 23 / 13 / 5 minutes before entering today's forecast segment
(lead times come from the protocol seed). Inside a segment the stage is 3 (stay seated).
"""
from __future__ import annotations

import math

Z_LOW = 1.28          # one-sided 10 % quantile of a normal distribution
GS_FLOOR_KT = 200.0


def position_sigma_nm(aid_ok: bool, est_d: float, loss_d: float) -> float:
    """1σ along-track position error: GNSS via AID vs dead reckoning after signal loss."""
    return 0.05 if aid_ok else 0.2 + 0.02 * (est_d - loss_d)


def forecast_sigma_nm(age_s: float) -> float:
    """Forecast placement error grows with the age of the last uplink."""
    return 5 + 3 * age_s / 3600


def tta_estimate(t_entry_s: float | None, in_segment: bool, gs_kt: float, pos_sigma_nm: float, forecast_age_s: float) -> dict | None:
    """Mean / σ / Q_low of the time until the segment. None when nothing is ahead in the look-ahead window."""
    if in_segment:
        return {"tta_mean_s": 0.0, "tta_sigma_s": 0.0, "q_low_s": 0.0}
    if t_entry_s is None:
        return None
    sigma_nm = math.hypot(forecast_sigma_nm(forecast_age_s), pos_sigma_nm)
    sigma_s = sigma_nm / max(gs_kt, GS_FLOOR_KT) * 3600
    return {"tta_mean_s": t_entry_s, "tta_sigma_s": sigma_s, "q_low_s": max(0.0, t_entry_s - Z_LOW * sigma_s)}


def point_stage(t_entry_s: float | None, in_segment: bool, leads: list[float]) -> int:
    """0 = normal, 1–3 = point stage. leads = [P1, P2, P3] lead times in seconds (descending)."""
    if in_segment:
        return 3
    if t_entry_s is None:
        return 0
    if t_entry_s <= leads[2]:
        return 3
    if t_entry_s <= leads[1]:
        return 2
    if t_entry_s <= leads[0]:
        return 1
    return 0


def point_timeline(exposure_t0: float, leads: list[float]) -> list[dict]:
    """Sim times at which P1–P3 start for a segment entered at exposure_t0."""
    return [{"point": i + 1, "t_s": exposure_t0 - lead} for i, lead in enumerate(leads)]
