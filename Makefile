.PHONY: dev install deploy freeze

install:
	uv sync

dev:
	PYTHONPATH=src ADMIN_TOKEN=dev-token uvicorn server:app --reload --port 8000

freeze:
	uv export --no-dev --no-hashes -o src/requirements.txt

deploy:
	sam build && sam deploy --guided
