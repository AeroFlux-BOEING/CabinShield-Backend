"""Optional LLM-written insight text (Anthropic Messages API).

Only rewrites the explanation shown in the app's insight panel. It never changes points, timings
or kernel decisions. Any failure (no key, timeout, bad JSON) falls back to the rule text.
"""
from __future__ import annotations

import json
import logging

import httpx

from app.config import Settings

log = logging.getLogger("cabinshield.llm")
API_URL = "https://api.anthropic.com/v1/messages"

PROMPT = """You write one short insight for flight attendants about an upcoming forecast turbulence segment.
Facts (JSON): {facts}
Rules: calm, practical, no speculation beyond the facts, never mention probability of injury, 2 sentences max.
Return JSON only: {{"ko": "<Korean>", "en": "<English>"}}"""


async def llm_insights(settings: Settings, rule: list[dict], facts: list[dict]) -> list[dict]:
    if not (settings.anthropic_api_key and settings.anthropic_model):
        return rule
    out = []
    async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as client:
        for r, f in zip(rule, facts):
            try:
                resp = await client.post(API_URL, headers={
                    "x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json",
                }, json={"model": settings.anthropic_model, "max_tokens": 300,
                         "messages": [{"role": "user", "content": PROMPT.format(facts=json.dumps(f, ensure_ascii=False))}]})
                resp.raise_for_status()
                text = resp.json()["content"][0]["text"]
                txt = json.loads(text[text.find("{"): text.rfind("}") + 1])
                out.append({**r, "source": "llm", "text": {"ko": str(txt["ko"]), "en": str(txt["en"])}})
            except Exception as exc:  # noqa: BLE001 — any failure falls back to rules
                log.warning("LLM insight failed, using rule text: %s", exc)
                out.append(r)
    return out
