"""CSM(Cabin Safety Margin) = Q_low(TTA) − Q_high(CST) − B"""
from app.models import Band


def compute_csm(tta_low_s: float, cst_high_s: float, buffer_s: float) -> float:
    return tta_low_s - cst_high_s - buffer_s   # 반올림은 표시 단계에서


def band_for(csm_s: float, green_s: float) -> Band:
    # 설명용 구간. 실제 임계값은 항공사 SOP·검증데이터로 결정.
    if csm_s >= green_s:
        return Band.GREEN
    if csm_s >= 0:
        return Band.AMBER
    return Band.RED
