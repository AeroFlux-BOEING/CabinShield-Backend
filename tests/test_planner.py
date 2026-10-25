import asyncio

from app.config import Settings
from app.planner import make_planner
from app.planner.llm import LlmPlanner, _extract_json
from app.planner.rule_based import RuleBasedPlanner
from app.scenarios import a320_hot_beverage


def test_make_planner_defaults_to_rule_without_key():
    assert isinstance(make_planner(Settings(planner="llm")), RuleBasedPlanner)


def test_llm_failure_falls_back_to_rule(monkeypatch):
    p = LlmPlanner(Settings(planner="llm", anthropic_api_key="x", anthropic_model="m"))

    async def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(p, "_call", boom)
    plans = asyncio.run(p.propose(a320_hot_beverage(), 60))
    assert {pl.plan_id for pl in plans} == {"RULE-FULL", "RULE-MINIMAL"}


def test_extract_json_tolerates_prose():
    d = _extract_json('후보입니다:\n```json\n{"plans": []}\n```')
    assert d == {"plans": []}
