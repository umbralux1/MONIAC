# ═══════════════════════════════════════════════════════════════
#  MONIAC — гидравлический компьютер Филлипса на FastAPI
# ═══════════════════════════════════════════════════════════════

PYTHON      ?= python3
VENV        ?= .venv
PIP         := $(VENV)/bin/pip
PY          := $(VENV)/bin/python
UVICORN     := $(VENV)/bin/uvicorn
HOST        ?= 0.0.0.0
PORT        ?= 8000
WORKERS     ?= 2

.DEFAULT_GOAL := help
.PHONY: help venv install install-dev run run-prod dev reload \
        test lint format typecheck check clean clean-venv \
        freeze tree health docker-build docker-run

# ───────────────────────────────────────────────────────────────
#  Справка
# ───────────────────────────────────────────────────────────────
help: ## Показать список команд
	@echo ""
	@echo "  MONIAC — доступные команды:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Переменные: HOST=$(HOST)  PORT=$(PORT)  WORKERS=$(WORKERS)"
	@echo ""

# ───────────────────────────────────────────────────────────────
#  Окружение
# ───────────────────────────────────────────────────────────────
venv: ## Создать виртуальное окружение
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)
	@echo "✓ venv готов: $(VENV)"

install: venv ## Установить production-зависимости
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "✓ зависимости установлены"

install-dev: venv ## Установить зависимости + dev-инструменты
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt
	@echo "✓ dev-окружение готово"

freeze: ## Зафиксировать текущие версии в requirements.lock
	$(PIP) freeze > requirements.lock
	@echo "✓ requirements.lock обновлён"

# ───────────────────────────────────────────────────────────────
#  Запуск
# ───────────────────────────────────────────────────────────────
run: install ## Запустить production-сервер (без reload)
	$(UVICORN) main:app --host $(HOST) --port $(PORT)

dev: install-dev ## Запустить dev-сервер с автоперезагрузкой
	$(UVICORN) main:app --reload --host $(HOST) --port $(PORT)

reload: dev ## Псевдоним для dev

run-prod: install ## Запустить через gunicorn с несколькими воркерами
	$(PIP) install gunicorn
	$(VENV)/bin/gunicorn main:app \
		-k uvicorn.workers.UvicornWorker \
		-w $(WORKERS) \
		-b $(HOST):$(PORT)

health: ## Проверить, что сервер отвечает
	@curl -s http://localhost:$(PORT)/api/health || \
		echo "✗ сервер не отвечает на порту $(PORT)"

# ───────────────────────────────────────────────────────────────
#  Качество кода
# ───────────────────────────────────────────────────────────────
lint: install-dev ## Проверить стиль через ruff
	$(VENV)/bin/ruff check .

format: install-dev ## Отформатировать код через ruff
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check --fix .

typecheck: install-dev ## Проверить типы через mypy
	$(VENV)/bin/mypy main.py

test: install-dev ## Запустить тесты
	$(VENV)/bin/pytest -q

check: lint typecheck test ## Полная проверка: линт + типы + тесты
	@echo "✓ все проверки пройдены"

# ───────────────────────────────────────────────────────────────
#  Утилиты
# ───────────────────────────────────────────────────────────────
tree: ## Показать структуру проекта
	@find . -type f \
		-not -path './$(VENV)/*' \
		-not -path './__pycache__/*' \
		-not -path './.git/*' \
		-not -path './.pytest_cache/*' \
		-not -path './.mypy_cache/*' \
		-not -path './.ruff_cache/*' \
		| sort

clean: ## Удалить кэши и временные файлы
	@find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name '.pytest_cache' -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name '.mypy_cache' -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name '.ruff_cache' -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name '*.pyc' -delete 2>/dev/null || true
	@echo "✓ кэши очищены"

clean-venv: ## Полностью удалить виртуальное окружение
	rm -rf $(VENV)
	@echo "✓ venv удалён"

# ───────────────────────────────────────────────────────────────
#  Docker (опционально)
# ───────────────────────────────────────────────────────────────
docker-build: ## Собрать Docker-образ
	docker build -t moniac:latest .

docker-run: ## Запустить контейнер
	docker run --rm -p $(PORT):8000 moniac:latest