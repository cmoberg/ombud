.PHONY: dev install deploy deploy-guided freeze

install:
	uv sync

dev:
	@pids=$$(lsof -ti tcp:8000 -sTCP:LISTEN 2>/dev/null); \
	if [ -n "$$pids" ]; then \
		echo "Stopping existing process(es) on port 8000: $$pids"; \
		kill $$pids; \
		sleep 1; \
	fi
	PYTHONPATH=src ADMIN_TOKEN=dev-token .venv/bin/uvicorn server:app --reload --port 8000

freeze:
	uv export --no-dev --no-hashes -o src/requirements.txt

deploy:
	sam build && sam deploy

deploy-guided:
	sam build && sam deploy --guided
