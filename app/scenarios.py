"""Demo scenarios for rehearsals and the presentation.

Each scenario is a list of steps applied to a running flight session via
POST /api/v1/flights/{id}/scenarios/{name}. Values are illustrative.
"""
from __future__ import annotations

POPUP_HAZARD = {
    "id": "HZ-POPUP",
    "name": {"ko": "캄차카 동쪽 · 새 청천난류 보고", "en": "East of Kamchatka · new clear-air turbulence report"},
    "fl": [320, 400], "f": 0.33, "off_nm": 10, "r_nm": 90, "drift_kt": 20,
    "shape": [1, 0.9, 1.1, 0.85, 1.05, 0.95, 1.1, 0.9],
}

SCENARIOS: dict[str, dict] = {
    "jump-next": {
        "title": {"ko": "다음 구간 30분 전으로 이동", "en": "Jump to 30 min before the next segment"},
        "steps": [{"op": "jump_next"}],
    },
    "aid-loss": {
        "title": {"ko": "위치 신호 끊김 → 예측 위치로 계속 판단", "en": "Position signal lost → keep deciding on estimate"},
        "steps": [{"op": "jump_next"}, {"op": "control", "aid": False}],
    },
    "link-loss": {
        "title": {"ko": "지상 연결 끊김 → 태블릿 단독 판단, 복구 시 일괄 동기화", "en": "Ground link lost → tablet decides alone, syncs on restore"},
        "steps": [{"op": "control", "sat": False}],
    },
    "popup-hazard": {
        "title": {"ko": "운항관리실이 새 난기류 구간을 발행", "en": "Ops publishes a new turbulence area"},
        "steps": [{"op": "add_hazard", "hazard": POPUP_HAZARD}],
    },
    "restore": {
        "title": {"ko": "신호·연결 복구", "en": "Restore signal and link"},
        "steps": [{"op": "control", "aid": True, "sat": True}],
    },
}
