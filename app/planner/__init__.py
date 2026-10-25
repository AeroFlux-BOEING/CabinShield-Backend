from app.config import Settings


def make_planner(settings: Settings):
    if settings.planner == "llm" and settings.anthropic_api_key and settings.anthropic_model:
        from app.planner.llm import LlmPlanner
        return LlmPlanner(settings)
    from app.planner.rule_based import RuleBasedPlanner
    return RuleBasedPlanner()
