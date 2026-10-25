"""대표 시나리오 프리셋 (팀원설명자료 6장 가상 예시 기준)."""
from app.models import (CabinState, Cart, CartStatus, Crew, Galley, Lavatory, ServiceType, Zone)


def a320_hot_beverage() -> CabinState:
    return CabinState(
        aircraft_type="A320",
        service_type=ServiceType.HOT_BEVERAGE,
        service_active=True,
        aisle_congestion=0.3,
        crew=[
            Crew(id="FA1", zone=Zone.MID),
            Crew(id="FA2", zone=Zone.MID),
            Crew(id="FA3", zone=Zone.AFT),
            Crew(id="FA4", zone=Zone.FWD, role="PURSER"),
        ],
        carts=[Cart(id="CART1", zone=Zone.MID, status=CartStatus.AISLE_UNSECURED, hot_liquid=True)],
        lavatories=[Lavatory(id="LAV-FWD", zone=Zone.FWD, occupied=False),
                    Lavatory(id="LAV-AFT", zone=Zone.AFT, occupied=True)],
        galleys=[Galley(zone=Zone.FWD, secured=True), Galley(zone=Zone.AFT, secured=False)],
    )


SCENARIOS = {
    "A320_HOT_BEVERAGE": {"cabin": a320_hot_beverage, "initial_tta_s": 140.0},
}
