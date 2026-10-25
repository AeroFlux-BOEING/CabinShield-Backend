"""런타임 설정. 모든 값은 환경변수(CS_ 접두사)로 덮어쓸 수 있다.

주의: 아래 임계값·분위수·버퍼는 PoC 시연용 기본값이며 항공업계 표준값이 아니다.
실제 값은 항공사 SOP와 검증데이터로 정해야 한다.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CS_", env_file=".env", extra="ignore")

    # --- 서버 ---
    env: str = "dev"
    api_key: str = ""                      # 비어 있으면 인증 비활성(로컬 개발용)
    cors_origins: str = "*"                # 콤마 구분
    data_dir: str = "./data"               # SQLite 감사로그 위치

    # --- Safety Kernel / CSM ---
    safety_buffer_s: float = 10.0          # B
    tta_quantile_low: float = 0.10         # Q_low(TTA)
    cst_quantile_high: float = 0.90        # Q_high(CST)
    mc_samples: int = 2000                 # 몬테카를로 샘플 수
    csm_green_s: float = 15.0              # CSM >= 이 값이면 GREEN (설명용 임계값)
    csm_preferred_min_s: float = 5.0       # 이보다 여유가 얇은 PASS 후보는 후순위
    ack_delay_median_s: float = 3.0        # 승무원 인지·확인 지연
    advisory_stale_s: float = 10.0         # TTA 정보가 이보다 오래되면 Fail-safe
    cabin_state_stale_s: float = 600.0     # 객실상태가 이보다 오래되면 Fail-safe
    stale_extra_buffer_s: float = 15.0     # Fail-safe 시 추가 버퍼

    # --- 시뮬레이터 ---
    sim_tick_s: float = 1.0                # 실시간 tick 간격
    sim_speed: float = 1.0                 # 배속 (발표 리허설용, 1.0=실시간)
    sim_lookahead_s: float = 120.0         # 이 시간 안에 위험구간이 들어오면 Advisory 발행

    # --- 생성형 AI Planner ---
    planner: str = "rule"                  # rule | llm
    anthropic_api_key: str = ""
    anthropic_model: str = ""              # 예: claude-sonnet-4-5 (콘솔에서 사용 가능한 모델 ID)
    llm_timeout_s: float = 8.0

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
