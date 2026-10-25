"""도메인 모델 (API 스키마 겸용)."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 객실 상태
class Zone(str, Enum):
    FWD = "FWD"
    MID = "MID"
    AFT = "AFT"


ZONE_INDEX = {Zone.FWD: 0, Zone.MID: 1, Zone.AFT: 2}
GALLEY_ZONES = (Zone.FWD, Zone.AFT)  # A320급 단일통로: 전/후방 갤리


class ServiceType(str, Enum):
    NONE = "NONE"
    HOT_BEVERAGE = "HOT_BEVERAGE"
    COLD_BEVERAGE = "COLD_BEVERAGE"
    MEAL = "MEAL"


class CartStatus(str, Enum):
    STOWED = "STOWED"                      # 갤리에 수납·잠금
    AISLE_UNSECURED = "AISLE_UNSECURED"    # 통로, 미고정
    AISLE_SECURED = "AISLE_SECURED"        # 통로, 브레이크 고정 (잔여위험 낮음)


class Crew(BaseModel):
    id: str
    zone: Zone
    role: str = "FA"


class Cart(BaseModel):
    id: str
    zone: Zone
    status: CartStatus = CartStatus.AISLE_UNSECURED
    hot_liquid: bool = False               # 이 카트에 뜨거운 음료/포트가 있는가


class Lavatory(BaseModel):
    id: str
    zone: Zone
    occupied: bool = False


class Galley(BaseModel):
    zone: Zone
    secured: bool = True


class CabinState(BaseModel):
    aircraft_type: str = "A320"
    service_type: ServiceType = ServiceType.NONE
    service_active: bool = False
    aisle_congestion: float = Field(0.0, ge=0.0, le=1.0)  # 0=한산, 1=매우 혼잡
    crew: list[Crew]
    carts: list[Cart] = []
    lavatories: list[Lavatory] = []
    galleys: list[Galley] = [Galley(zone=Zone.FWD), Galley(zone=Zone.AFT)]
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- 난기류 / TTA
class TtaSource(str, Enum):
    SIMULATOR = "SIMULATOR"
    OPS_FEED = "OPS_FEED"      # 조종실/운항통제 등 승인된 외부 정보원 (향후)


class TtaEstimate(BaseModel):
    """TTA는 단일값이 아니라 범위(분포)로 다룬다."""
    source: TtaSource
    advisory_id: str
    severity: Literal["LIGHT", "MODERATE", "SEVERE"] = "MODERATE"
    tta_mean_s: float
    tta_sigma_s: float = Field(..., ge=0)
    tta_low_s: float            # Q_low(TTA) – 보수적 하한
    tta_high_s: float
    issued_at: datetime = Field(default_factory=utcnow)


class AdvisoryIn(BaseModel):
    """외부 운항계층이 보내는 난기류 위험 이벤트 (OPS_FEED ingest)."""
    advisory_id: str
    severity: Literal["LIGHT", "MODERATE", "SEVERE"] = "MODERATE"
    tta_mean_s: float = Field(..., gt=0)
    tta_sigma_s: float = Field(5.0, ge=0)


# ---------------------------------------------------------------- 계획 / 판정
class PlanStep(BaseModel):
    action_id: str
    crew_id: str
    target_id: Optional[str] = None    # 카트/화장실 id, 갤리는 zone 문자열


class Plan(BaseModel):
    plan_id: str
    source: Literal["RULE", "LLM", "KERNEL_FALLBACK"]
    label: str
    steps: list[PlanStep]
    rationale: str = ""


class Verdict(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"


class Band(str, Enum):
    GREEN = "GREEN"     # 정상 안전전환
    AMBER = "AMBER"     # 비필수 업무 축소
    RED = "RED"         # 자기보호 우선


class CstResult(BaseModel):
    cst_median_s: float
    cst_high_s: float           # Q_high(CST)
    per_crew_finish_s: dict[str, float]
    timeline: list[dict]        # 단계별 예상 시작/종료(중앙값 기준)


class PlanEvaluation(BaseModel):
    plan: Plan
    verdict: Verdict
    reasons: list[str] = []
    cst: Optional[CstResult] = None
    csm_s: Optional[float] = None
    residual_hazard: float = 0.0
    residual_items: list[str] = []


class Decision(BaseModel):
    flight_id: str
    computed_at: datetime = Field(default_factory=utcnow)
    tta: Optional[TtaEstimate]
    buffer_s: float
    fail_safe: bool = False
    fail_safe_reasons: list[str] = []
    band: Optional[Band] = None
    recommended: Optional[PlanEvaluation] = None
    candidates: list[PlanEvaluation] = []


class EvaluateRequest(BaseModel):
    """상태 비저장 평가 (콘솔·테스트용)."""
    cabin: CabinState
    tta_mean_s: float = Field(..., gt=0)
    tta_sigma_s: float = Field(5.0, ge=0)
