"""Runtime settings. Every value can be overridden with a CS_* environment variable.

Thresholds, buffers and timings are demo defaults for the PoC, not aviation standards.
Real values must come from the airline's SOPs and validation data.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CS_", env_file=".env", extra="ignore")

    # --- server ---
    env: str = "dev"
    api_key: str = ""                  # empty = auth disabled (local dev). Tablet / ops send X-API-Key
    ops_api_key: str = ""              # optional separate key for forecast ingest & ops endpoints
    cors_origins: str = "*"            # comma separated
    data_dir: str = "./data"           # SQLite location

    # --- safety kernel (mirrors the tablet) ---
    safety_buffer_s: float = 10.0      # B in CSM = Q_low − (CST + margin) − B
    uplink_interval_s: float = 900.0   # forecast uplink cadence while the satellite link is up (sim s)

    # --- flight simulator (demo "aircraft feed") ---
    sim_tick_s: float = 1.0            # real seconds between ticks
    sim_speed: float = 60.0            # sim seconds per real second (10 / 60 / 300 in the app)
    sim_slow_near_s: float = 1560.0    # within this of a segment, cap speed at sim_slow_speed
    sim_slow_speed: float = 10.0
    state_push_s: float = 1.0          # WS state push throttle (real s)

    # --- live aviation weather (NWS Aviation Weather Center, aviationweather.gov) ---
    wx_mode: str = "live"              # live | fixture | off
    wx_routes: str = "ICN-SFO"         # comma separated routes to keep updated
    wx_interval_s: float = 600.0       # refresh cadence (real s); AWC allows ≤ 100 req/min, we use 3 per refresh
    wx_pirep_age_h: float = 6.0        # PIREP/AIREP look-back
    wx_corridor_nm: float = 120.0      # keep reports/areas within this distance of the route
    wx_pirep_hazards: bool = True      # turn clusters of MOD+ reports into short-lived hazards
    wx_keep_seed: bool = True          # keep the demo forecast areas next to live ones
    wx_time_mode: str = "replay"       # replay: live areas appear 'now' in sim time · real: true UTC validity
    wx_contact: str = ""               # appended to the User-Agent (AWC asks for a custom UA)
    wx_fixture_dir: str = ""           # saved API responses for wx_mode=fixture

    # --- insight text ---
    planner: str = "rule"              # rule | llm
    anthropic_api_key: str = ""
    anthropic_model: str = ""          # model ID available in your Anthropic console
    llm_timeout_s: float = 8.0

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
