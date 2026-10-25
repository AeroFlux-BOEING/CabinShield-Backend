"""생성형 AI 후보 생성기 (Anthropic Messages API).

- AI는 승인 행동 라이브러리의 action_id만 사용하도록 프롬프트로 제한한다.
- 하지만 신뢰하지 않는다: 출력은 반드시 Safety Kernel이 다시 검증한다.
- 호출 실패/파싱 실패 시 규칙 기반 후보로 폴백한다.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from app.config import Settings
from app.library import library_as_dict
from app.models import CabinState, Plan, PlanStep
from app.planner.rule_based import RuleBasedPlanner

log = logging.getLogger("cabinshield.llm")

SYSTEM = """너는 객실승무원 난기류 대응 '후보 계획'을 만드는 보조 모듈이다.
규칙:
1. 반드시 제공된 ACTION LIBRARY의 action_id만 사용한다. 새 절차를 만들지 않는다.
2. 각 승무원(crew_id)은 마지막 행동으로 CREW_SELF_PROTECT를 정확히 1번 수행해야 한다.
3. 너는 안전을 판정하지 않는다. 시간·규칙 검증은 별도 Safety Kernel이 한다.
4. 서로 다른 전략의 후보 2~3개를 만든다(예: 완전정리 / 핵심만 / 즉시착석).
5. 출력은 JSON 하나만: {"plans":[{"label":str,"rationale":str(한국어 1~2문장),
   "steps":[{"action_id":str,"crew_id":str,"target_id":str|null}]}]}
   target_id: CART→카트 id, LAV→화장실 id, GALLEY/ZONE→"FWD"|"MID"|"AFT", NONE→null
"""


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in LLM output")
    return json.loads(m.group(0))


class LlmPlanner:
    name = "llm"

    def __init__(self, settings: Settings):
        self.s = settings
        self.fallback = RuleBasedPlanner()

    async def propose(self, cabin: CabinState, tta_low_s: float | None = None) -> list[Plan]:
        try:
            plans = await self._call(cabin, tta_low_s)
        except Exception as e:  # noqa: BLE001 — 어떤 실패든 폴백
            log.warning("LLM planner failed, falling back to rule planner: %s", e)
            return await self.fallback.propose(cabin, tta_low_s)
        return plans   # 즉시 자기보호 후보는 커널이 항상 추가

    async def _call(self, cabin: CabinState, tta_low_s: float | None) -> list[Plan]:
        user = {
            "conservative_tta_s": tta_low_s,
            "cabin_state": json.loads(cabin.model_dump_json(exclude={"updated_at"})),
            "action_library": library_as_dict(),
        }
        async with httpx.AsyncClient(timeout=self.s.llm_timeout_s) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": self.s.anthropic_api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": self.s.anthropic_model, "max_tokens": 1500, "system": SYSTEM,
                      "messages": [{"role": "user", "content": json.dumps(user, ensure_ascii=False)}]},
            )
            r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        data = _extract_json(text)
        out: list[Plan] = []
        for i, p in enumerate(data.get("plans", [])[:3]):
            out.append(Plan(
                plan_id=f"LLM-{chr(65 + i)}", source="LLM", label=str(p.get("label", f"AI 후보 {i+1}")),
                rationale=str(p.get("rationale", "")),
                steps=[PlanStep(**s) for s in p.get("steps", [])],
            ))
        if not out:
            raise ValueError("LLM returned no plans")
        return out
