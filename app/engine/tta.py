"""TTA(Time to Turbulence) 산정.

PoC: 가상 항공기(along-track 1차원)와 가상 위험구간 사이 거리·속도로 TTA를 계산.
승무원 입력이 아니라 비행/운항 계층에서 자동 산출·전달되는 구조를 흉내낸다.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from statistics import NormalDist

from app.models import TtaEstimate, TtaSource


def tta_estimate(mean_s: float, sigma_s: float, q_low: float, *, source: TtaSource,
                 advisory_id: str, severity: str = "MODERATE") -> TtaEstimate:
    nd = NormalDist()
    z_low = nd.inv_cdf(q_low)
    z_high = nd.inv_cdf(1 - q_low)
    return TtaEstimate(
        source=source, advisory_id=advisory_id, severity=severity,
        tta_mean_s=round(mean_s, 2), tta_sigma_s=round(sigma_s, 2),
        tta_low_s=round(max(0.0, mean_s + z_low * sigma_s), 2),
        tta_high_s=round(max(0.0, mean_s + z_high * sigma_s), 2),
    )


@dataclass
class FlightSimulator:
    """1차원 along-track 비행 시뮬레이터."""
    ground_speed_mps: float = 230.0          # ≈ 447 kt
    gs_sigma_frac: float = 0.02              # 대지속도 불확실성(비율)
    zone_start_m: float = 32_200.0           # 위험구간 시작점(경로상 거리)
    zone_length_m: float = 40_000.0
    zone_pos_sigma_m: float = 1_500.0        # 예보 위치오차
    severity: str = "MODERATE"
    position_m: float = 0.0
    elapsed_s: float = 0.0
    advisory_id: str = field(default_factory=lambda: f"ADV-{uuid.uuid4().hex[:6].upper()}")

    def step(self, dt: float) -> None:
        self.position_m += self.ground_speed_mps * dt
        self.elapsed_s += dt

    @property
    def distance_to_zone_m(self) -> float:
        return self.zone_start_m - self.position_m

    @property
    def in_zone(self) -> bool:
        return self.zone_start_m <= self.position_m <= self.zone_start_m + self.zone_length_m

    @property
    def passed_zone(self) -> bool:
        return self.position_m > self.zone_start_m + self.zone_length_m

    def raw_tta(self) -> tuple[float, float]:
        d = self.distance_to_zone_m
        mean = max(0.0, d / self.ground_speed_mps)
        sigma = math.hypot(self.zone_pos_sigma_m / self.ground_speed_mps, mean * self.gs_sigma_frac)
        return mean, sigma

    def estimate(self, q_low: float) -> TtaEstimate:
        mean, sigma = self.raw_tta()
        return tta_estimate(mean, sigma, q_low, source=TtaSource.SIMULATOR,
                            advisory_id=self.advisory_id, severity=self.severity)

    def snapshot(self) -> dict:
        mean, sigma = self.raw_tta()
        return {
            "elapsed_s": round(self.elapsed_s, 1),
            "position_m": round(self.position_m, 1),
            "ground_speed_mps": self.ground_speed_mps,
            "distance_to_zone_m": round(self.distance_to_zone_m, 1),
            "raw_tta_mean_s": round(mean, 1),
            "raw_tta_sigma_s": round(sigma, 2),
            "in_zone": self.in_zone,
            "passed_zone": self.passed_zone,
        }

    @classmethod
    def from_initial_tta(cls, tta_s: float, **kw) -> "FlightSimulator":
        gs = kw.pop("ground_speed_mps", 230.0)
        return cls(ground_speed_mps=gs, zone_start_m=tta_s * gs, **kw)
