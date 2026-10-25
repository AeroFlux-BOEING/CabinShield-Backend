"""승인된 행동 라이브러리.

생성형 AI는 여기 정의된 action_id만 조합할 수 있다. 소요시간은 로그정규 분포
(median, sigma)로 관리하며, 값은 팀 측정 전 설명용 placeholder다.
→ 실제 측정 후 `data/action_times.json` 등으로 교체할 것.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TargetKind = Literal["NONE", "CART", "LAV", "GALLEY", "ZONE"]


@dataclass(frozen=True)
class ActionDef:
    id: str
    name_ko: str
    target: TargetKind
    median_s: float
    sigma: float = 0.2                       # lognormal sigma
    moves_crew_to_target: bool = True        # 수행 위치로 이동해야 하는가
    terminal: bool = False                   # 자기보호(이후 다른 행동 금지)
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


TRAVEL_PER_ZONE = ActionDef("_TRAVEL", "구역 이동", "NONE", 6.0, 0.25, False)
CART_TRAVEL_PER_ZONE_S = 10.0                # 카트 밀고 이동 시 구역당 추가시간(median)
ACK_DELAY = ActionDef("_ACK", "경고 인지·확인", "NONE", 3.0, 0.3, False)

ACTIONS: dict[str, ActionDef] = {a.id: a for a in [
    ActionDef("STOP_SERVICE", "서비스 즉시 중단", "NONE", 2.0, 0.2, False,
              description="진행 중 서비스를 중단하고 승객 응대를 멈춘다."),
    ActionDef("STOW_HOT_LIQUID", "뜨거운 음료 수납", "CART", 7.0, 0.2,
              description="카트 위 포트·컵을 카트 내부에 수납."),
    ActionDef("SECURE_CART_IN_PLACE", "카트 현장 고정", "CART", 6.0, 0.2,
              description="통로에서 브레이크 체결 + 승객 좌석 측 고정."),
    ActionDef("RETURN_CART_TO_GALLEY", "카트 갤리 복귀·잠금", "CART", 8.0, 0.25,
              description="가장 가까운 갤리로 카트를 이동 후 잠금. 이동시간 별도 가산."),
    ActionDef("LAV_PAX_GUIDANCE", "화장실 승객 안내", "LAV", 10.0, 0.3,
              description="노크 후 즉시 착석/손잡이 파지 안내."),
    ActionDef("SECURE_GALLEY", "갤리 정리·잠금", "GALLEY", 12.0, 0.25,
              description="갤리 컨테이너·도어 잠금."),
    ActionDef("PA_SEATBELT", "좌석벨트 PA", "NONE", 5.0, 0.15, False,
              description="가까운 핸드셋으로 좌석벨트 착용 방송."),
    ActionDef("CABIN_CHECK_ZONE", "구역 좌석벨트 확인", "ZONE", 8.0, 0.25,
              description="담당 구역 승객 착용 육안 확인."),
    ActionDef("CREW_SELF_PROTECT", "승무원 자기보호(착석)", "NONE", 5.0, 0.2, False, True,
              description="가장 가까운 점프싯/빈 좌석 착석·벨트 체결. 마지막 행동이어야 함."),
]}


# 잔여위험 가중치 (설명용). Safety Kernel이 후보 비교에 사용.
HAZARD_WEIGHTS = {
    "HOT_LIQUID": 3.0,
    "CART_AISLE_UNSECURED": 4.0,
    "CART_AISLE_SECURED": 1.0,
    "LAV_OCCUPIED": 2.0,
    "GALLEY_UNSECURED": 1.0,
    "SERVICE_ACTIVE": 0.5,
}


def library_as_dict() -> list[dict]:
    return [
        {"id": a.id, "name_ko": a.name_ko, "target": a.target, "median_s": a.median_s,
         "sigma": a.sigma, "terminal": a.terminal, "description": a.description}
        for a in ACTIONS.values()
    ]
