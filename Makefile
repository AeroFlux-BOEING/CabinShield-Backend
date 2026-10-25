.PHONY: dev test up down
dev:   ; uvicorn app.main:app --reload --port 8000
test:  ; pytest -q
up:    ; cp -n .env.example .env 2>/dev/null; docker compose up --build -d
down:  ; docker compose down
