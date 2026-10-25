# CabinShield-SM Backend

난기류 경고 이후 **TTA(난기류까지 남은 시간)** 와 **CST(객실 안전전환 시간)** 를 비교해 **CSM(안전여유)** 을 계산하고,
생성형 AI 후보계획을 **Safety Kernel**이 검증해 승무원에게 "무엇까지 하고 언제 착석할지"를 실시간 푸시하는 PoC 서버.

- 스택: Python 3.12 · FastAPI · numpy(몬테카를로) · WebSocket · SQLite 감사로그
- 배포: Docker → Render 또는 Fly.io
- 설계 문서: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

## 빠른 시작

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
make dev            # http://localhost:8000/docs  (Swagger)
make test
```

### 데모 시나리오 (curl)

```bash
# 1) 대표 시나리오 세션 생성 (A320, HOT_BEVERAGE, 카트 MID 미고정, 후방 화장실 점유, 초기 TTA 140s)
curl -X POST localhost:8000/api/v1/flights -H 'content-type: application/json' \
     -d '{"flight_id":"KE001","autostart":true}'

# 2) 실시간 스트림 구독 (승무원 태블릿 역할)
npx wscat -c ws://localhost:8000/ws/flights/KE001

# 발표용: 일시정지 후 수동 진행
curl -X POST localhost:8000/api/v1/flights/KE001/sim/stop
curl -X POST "localhost:8000/api/v1/flights/KE001/sim/step?seconds=10"

# 외부 운항계층 이벤트 주입 (시뮬레이터 대신)
curl -X POST localhost:8000/api/v1/flights/KE001/advisories -H 'content-type: application/json' \
     -d '{"advisory_id":"OCC-1","tta_mean_s":60,"tta_sigma_s":5}'
```

리허설은 `CS_SIM_SPEED=5`로 배속 가능.

## Docker

```bash
make up             # docker compose, ./data 에 감사로그 유지
```

## 배포

**Render**: 대시보드 → New → Blueprint → 레포 선택 (`render.yaml` 자동 인식). `CS_API_KEY`는 자동 생성됨.

**Fly.io**:
```bash
fly launch --no-deploy --copy-config      # app 이름 수정
fly volumes create cabinshield_data -r nrt -s 1
fly secrets set CS_API_KEY=$(openssl rand -hex 16)
fly deploy
```

생성형 AI 사용 시: `CS_PLANNER=llm`, `CS_ANTHROPIC_API_KEY`, `CS_ANTHROPIC_MODEL` 설정. 실패 시 규칙 기반 후보로 자동 폴백.

## 구조

```
app/
├─ main.py            FastAPI 앱, lifespan
├─ config.py          환경변수 설정 (CS_*)
├─ models.py          CabinState / TtaEstimate / Plan / Decision
├─ library.py         승인 행동 라이브러리 + 소요시간 분포 (← 측정값으로 교체)
├─ scenarios.py       대표 시나리오 프리셋
├─ engine/
│  ├─ tta.py          비행 시뮬레이터, TTA 범위 산정
│  ├─ cst.py          병렬 스케줄 × 몬테카를로 CST
│  └─ csm.py          CSM, 밴드
├─ planner/
│  ├─ rule_based.py   FULL / MINIMAL 후보
│  └─ llm.py          Anthropic API 후보 (커널이 재검증)
├─ kernel.py          Safety Kernel — 구조·규칙·시간 검증, Fail-safe
├─ orchestrator.py    세션, 시뮬레이터 루프, WS 팬아웃, CST 캐시
├─ store.py           감사로그
└─ api/routes.py      REST + WebSocket
```

> ⚠️ 모든 수치(행동시간, 분위수, 버퍼, 밴드 임계값)는 알고리즘 시연용 기본값이며 항공업계 표준이나 실제 승무원 성능이 아닙니다.
