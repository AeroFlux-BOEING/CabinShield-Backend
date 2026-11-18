# 배포 가이드 (Ground API · AI)

## 무엇을 어디에 배포하나

| 구성 요소 | 배포 위치 | 방법 |
|---|---|---|
| Ground API (FastAPI, 이 저장소) | Render 또는 Fly.io (Docker, 워커 1개) | `render.yaml` / `fly.toml` |
| 생성형 AI (인사이트 문구) | **별도 배포 없음.** Ground API 안에서 Anthropic API 호출 | 환경변수 3개 (아래) |
| 온디바이스 CST 모델 (MLP 1,827 파라미터) | **서버에서 추론하지 않음.** 모델 JSON을 Ground API가 배포하고 iPad가 받아서 실행 | CabinShield-AI에서 `make loop` → `make publish` |
| 학습 파이프라인 (CabinShield-AI) | 배포하지 않음 (로컬 PC 오프라인) | — |
| 크루 앱 웹 프리뷰 | Vercel | `CS_CORS_ORIGINS`에 Vercel 도메인 추가 |

판단(Safety Kernel + CST 모델)은 기내 iPad에서 해요. 서버는 데이터를 공급하고, 앱의 판단을 다시 검증해서 기록해요.

## 1. Ground API — Render (가장 간단)

1. Render 대시보드 → **New → Blueprint** → `CabinShield-Backend` 저장소 선택. `render.yaml`이 그대로 적용돼요.
2. 자동으로 설정되는 값: `CS_API_KEY`, `CS_OPS_API_KEY` (생성), `CS_WX_MODE=live`, `CS_PLANNER=rule`, `CS_WX_SIGWX=auto`
3. 직접 넣을 값 (대시보드 → Environment):

| 변수 | 값 |
|---|---|
| `CS_CORS_ORIGINS` | 예: `https://cabinshield.vercel.app` (쉼표로 여러 개) |
| `CS_WX_CONTACT` | 팀 메일 (AWC User-Agent에 붙음) |
| `CS_PLANNER` | 생성형 AI 문구를 쓰려면 `llm` |
| `CS_ANTHROPIC_API_KEY` | Anthropic Console에서 발급한 키 |
| `CS_ANTHROPIC_MODEL` | Console에서 쓸 수 있는 모델 ID |
| `CS_WIFS_API_KEY` | WIFS 승인 후에만. 비워두면 SIGWX는 꺼진 채로 나머지는 정상 동작 |

4. 배포 확인
```bash
curl https://<서비스>.onrender.com/healthz                 # ok, model 버전, wx 모드
curl https://<서비스>.onrender.com/api/v1/wx/status        # mode, sigwx_mode, 노선별 수집 결과
curl https://<서비스>.onrender.com/api/v1/models/cst       # 배포 중인 CST 모델 sha256
```

무료 플랜의 제약:
- 트래픽이 15분 없으면 잠들고, 다음 요청에 깨어나는 데 약 1분 걸려요. **발표 5분 전에 `/healthz`를 한 번 호출**해 두세요.
- 파일시스템이 임시라서 재배포·재시작·잠들 때마다 SQLite(비행편·sync·감사 로그)가 초기화돼요. 데모에는 문제없어요. 기록을 남겨야 하면 유료 인스턴스에 디스크를 붙이거나 Fly.io를 쓰세요.

## 2. Ground API — Fly.io (기록 보존이 필요할 때)

```bash
fly launch --no-deploy --copy-config          # app 이름만 바꾸기
fly volumes create cabinshield_data -r nrt -s 1
fly secrets set CS_API_KEY=... CS_OPS_API_KEY=... CS_CORS_ORIGINS=https://cabinshield.vercel.app
fly secrets set CS_PLANNER=llm CS_ANTHROPIC_API_KEY=... CS_ANTHROPIC_MODEL=...   # 생성형 AI (선택)
fly secrets set CS_WIFS_API_KEY=...                                              # SIGWX (승인 후)
fly deploy
```

`/srv/data` 볼륨에 SQLite가 남아요. 세션과 WebSocket 구독자가 메모리에 있으므로 머신은 1대만 운용해요(`min_machines_running = 1`).

## 3. 생성형 AI (LLM 인사이트)

- `app/planner/llm.py`가 Anthropic Messages API로 승무원용 안내 문구(한/영, 2문장)만 다시 써요.
- 포인트 시각, CSM, 커널 판정은 **절대 바꾸지 않아요**. 키가 없거나, 시간 초과(`CS_LLM_TIMEOUT_S`, 기본 8초)이거나, JSON이 깨지면 규칙 기반 문구로 자동 폴백해요.
- 켜기: `CS_PLANNER=llm` + `CS_ANTHROPIC_API_KEY` + `CS_ANTHROPIC_MODEL`. 서버를 다시 배포할 필요 없이 환경변수만 바꾸면 돼요(Render는 변경 시 자동 재시작).

## 4. 온디바이스 CST 모델 배포 (CabinShield-AI → Backend · Frontend)

```bash
cd CabinShield-AI
make loop       # 항로 시나리오로 학습·평가 반복 → artifacts/models/cst_mlp_v2.json, reports/cst-mlp-v2.json
make publish    # unsafe pick이 있으면 거부. Backend app/data/models/에 JSON 복사 + Frontend src/ai/cstWeights.ts 재생성
```
그다음 Backend와 Frontend 저장소에서 각각 커밋하고 push해요. Render는 자동 배포돼요.

- 앱은 출발 전에 `GET /api/v1/models/cst`의 `sha256`을 번들과 비교해요. 다를 때만 `/models/cst/{version}`을 내려받아요.
- **주의:** `get_model()`은 버전 문자열이 가장 큰 모델을 기본값으로 써요. 그래서 `cst_mlp_v2.json`을 넣는 순간 서버의 지상 재검증도 v2로 바뀌어요. 프론트 번들(`cstWeights.ts`)과 같이 바꾸고, `pytest`(golden 값 비교 포함)를 돌린 뒤 배포하세요.
- 지금 행동시간 분포는 임시값이에요(`configs/sim.yaml` `actions`). 팀 실측으로 바꾼 뒤 다시 `make loop` 하는 것을 권장해요.

## 5. 배포 체크리스트

- [ ] `pytest -q` 통과 (CI: `.github/workflows/ci.yml`이 테스트 + Docker 빌드)
- [ ] `CS_API_KEY` 설정 (prod에서 비어 있으면 쓰기 API가 인증 없이 열려요. 시작 로그에 경고가 떠요)
- [ ] `CS_CORS_ORIGINS`에 프론트 도메인
- [ ] `/healthz` → `model` 버전이 앱 번들과 같은지
- [ ] `/api/v1/wx/status` → `last_error`, `sigwx_error` 확인
