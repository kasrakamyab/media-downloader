.PHONY: dev test lint fmt build up down logs install

install:
	uv venv --allow-existing .venv
	uv pip install --python .venv/bin/python -e ".[dev]"

dev: ## run everything locally with Docker Compose (polling mode)
	docker compose up --build

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

fmt:
	.venv/bin/ruff check --fix . && .venv/bin/ruff format .

build:
	docker compose build
