"""Rule-based planners.

* `plan_services` — moves or simplifies cabin services so they do not overlap forecast segments
  (plus a lead-time dependent buffer). Started services are frozen. Port of `src/domain/planner.ts`.
* `rule_insights` — matches upcoming forecast segments with past similar reports and writes the
  short explanation shown in the app's "AI insight" panel.
"""
from __future__ import annotations

from app.engine.flight import Exposure

MODE_LABEL = {
    "normal": {"ko": "정상", "en": "Normal"},
    "simplified": {"ko": "간소화", "en": "Simplified"},
    "skipped": {"ko": "생략", "en": "Skipped"},
    "purser": {"ko": "사무장 결정", "en": "Purser decides"},
}


def buffer_before(e: Exposure, now: float) -> float:
    return 600 + 120 * max(0.0, (e.t0 - now) / 3600)


def plan_services(services: list[dict], now: float, eta: float, ex: list[Exposure], prev: list[dict] | None) -> list[dict]:
    latest = eta - 40 * 60
    blocks = [(e.t0 - buffer_before(e, now), e.t1 + 600) for e in ex]

    def free(a: float, b: float) -> bool:
        return all(b <= x or a >= y for x, y in blocks)

    prev_end = 0.0
    out: list[dict] = []
    for sv in services:
        old = next((q for q in prev or [] if q["id"] == sv["id"]), None)
        if old and old["start_s"] is not None and old["start_s"] <= now:
            prev_end = old["end_s"]
            out.append(old)
            continue
        if (old and old["start_s"] is not None and old["start_s"] >= prev_end + 1200 and old["end_s"] <= latest
                and free(old["start_s"], old["end_s"])):
            prev_end = old["end_s"]
            out.append(old)
            continue

        def try_mode(dur: float):
            for k in range(37):
                for sg in ((1, -1) if k else (1,)):
                    st = sv["pref_s"] + sg * k * 300
                    if st < now or st < prev_end + 1200 or st + dur > latest:
                        continue
                    if free(st, st + dur):
                        return st, sg * k * 300
            return None

        r, mode, dur = try_mode(sv["dur_s"]), "normal", sv["dur_s"]
        if sv.get("simplified_s") and (r is None or abs(r[1]) > 3600):
            r2 = try_mode(sv["simplified_s"])
            if r2 and (r is None or abs(r2[1]) < abs(r[1])):
                r, mode, dur = r2, "simplified", sv["simplified_s"]
        base = {"id": sv["id"], "name": sv["name"]}
        if r is None:
            out.append({**base, "start_s": None, "end_s": None, "mode": "skipped" if sv.get("optional") else "purser", "shift_s": 0})
            continue
        prev_end = r[0] + dur
        out.append({**base, "start_s": r[0], "end_s": r[0] + dur, "mode": mode, "shift_s": r[1]})
    return out


def match_history(e: Exposure, hist: list[dict]) -> dict | None:
    """Past report segment overlapping the exposure the most (by distance along the route)."""
    best, best_ov = None, 0.0
    for h in hist:
        ov = min(e.d1, h["d1_nm"]) - max(e.d0, h["d0_nm"])
        if ov > best_ov:
            best, best_ov = h, ov
    return best


def rule_insights(ex: list[Exposure], hist: list[dict], strength: list[dict]) -> list[dict]:
    out = []
    for e in ex:
        h = match_history(e, hist)
        s = strength[e.max]
        if h:
            text = {
                "ko": f"오늘 예보 구간({s['ko']})이 과거 보고가 {h['cases']}건 있었던 '{h['name']['ko']}' 구간과 겹칩니다. "
                      f"주요 원인은 {h['pattern']['ko']}이며 {h['severity']['ko']}였습니다({h['season']['ko']}). "
                      "예보보다 일찍 흔들릴 수 있으니 1포인트 준비를 서두르는 것이 좋아요.",
                "en": f"Today's forecast segment ({s['en']}) overlaps '{h['name']['en']}', where {h['cases']} reports were filed. "
                      f"Main cause: {h['pattern']['en']}; {h['severity']['en']} ({h['season']['en']}). "
                      "It may start earlier than forecast, so get Point 1 preparation going early.",
            }
        else:
            text = {
                "ko": f"오늘 예보 구간({s['ko']})과 겹치는 과거 보고는 없습니다. 예보 기준으로 포인트를 진행하세요.",
                "en": f"No past reports overlap today's forecast segment ({s['en']}). Follow the points as forecast.",
            }
        out.append({"hazard_id": e.hazard.id, "t0_s": e.t0, "history_id": h["id"] if h else None, "source": "rule", "text": text})
    return out
