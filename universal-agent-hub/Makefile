# ---------------------------------------------------------------------------
# Universal Agent Hub — common developer commands
#   make help          list everything
#   make install       create .venv and install runtime + dev dependencies
#   make test          run pytest with coverage (fails under 80%)
# ---------------------------------------------------------------------------
PYTHON      ?= python3
VENV        ?= .venv
BIN         := $(VENV)/bin
PY          := $(BIN)/python
PKG_NAME    := universal-agent-hub
VERSION     := 1.0.0
COVERAGE_MIN ?= 80

.DEFAULT_GOAL := help
.PHONY: help venv install install-deps lock fmt lint format-check typecheck mypy test test-cov \
        coverage-html run demo clean distclean build docker-build docker-run docker-shell publish \
        audit safety-check reset-env icons

help: ## Show this help
	@echo "Universal Agent Hub $(VERSION)"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_.-]+:.*?## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

$(VENV)/bin/python: ## Create the virtualenv
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip >/dev/null

venv: $(VENV)/bin/python ## Alias for creating the virtualenv

install: venv ## Install the package in editable mode with dev extras
	$(BIN)/pip install -r requirements.txt -r requirements-dev.txt
	$(BIN)/pip install -e ".[dev]"

install-deps: venv ## Install runtime dependencies only
	$(BIN)/pip install -r requirements.txt

fmt: ## Format with black and sort imports/fix lint with ruff
	$(BIN)/black src tests examples scripts
	$(BIN)/ruff check --fix src tests examples scripts
	$(BIN)/ruff format src tests examples scripts || true

lint: ## Run ruff + black check
	$(BIN)/ruff check src tests examples scripts
	$(BIN)/black --check src tests examples scripts

format-check: lint ## Alias kept for CI parity

typecheck: mypy ## Alias
mypy: ## Static type checking (mypy --strict)
	$(BIN)/mypy src scripts/make_icons.py

test: ## Run the test suite with coverage gate
	$(BIN)/pytest -q --cov-fail-under=$(COVERAGE_MIN)

test-fast: ## Run tests without coverage
	$(BIN)/pytest -q -p no:cacheprovider --no-cov

test-cov: ## Run tests and open an HTML coverage report
	$(BIN)/pytest -q --cov-report=html
	@echo "report: htmlcov/index.html"

coverage-html: test-cov ## Alias

run: ## Launch the interactive CLI
	$(BIN)/python -m src.cli

demo: ## Non-interactive smoke run (uses a fake plan, no API key needed)
	$(BIN)/python examples/basic_usage.py

safety-check: ## Show the resolved safety policy and the registered tools
	$(BIN)/python -m src.cli --dry-run

reset-env: ## Recreate .env from the example (destructive: overwrites .env)
	cp .env.example .env
	@echo "edit .env and set OPENAI_API_KEY"

build: ## Build sdist + wheel
	$(BIN)/pip install --upgrade build >/dev/null
	$(BIN)/python -m build

publish: build ## Upload to PyPI (twine must be configured)
	$(BIN)/pip install --upgrade twine >/dev/null
	$(BIN)/python -m twine upload dist/*

audit: ## Check installed packages for known vulnerabilities
	$(BIN)/pip install --upgrade pip-audit >/dev/null
	$(BIN)/python -m pip_audit

docker-build: ## Build the container image
	docker build -t $(PKG_NAME):$(VERSION) -t $(PKG_NAME):latest .

docker-run: ## Run the container (mounts the current directory as the workspace)
	docker run --rm -it \
		--env-file .env \
		-v "$$PWD:/app/workspace" \
		-w /app/workspace \
		$(PKG_NAME):latest

docker-shell: ## Debug shell inside the image
	docker run --rm -it --entrypoint bash $(PKG_NAME):latest

clean: ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov dist build *.egg-info
	find . -type d -name __pycache__ -not -path "./$(VENV)/*" -exec rm -rf {} +
	rm -f .coverage coverage.xml agent.log agent-events.jsonl

distclean: clean ## Also remove the virtualenv
	rm -rf $(VENV)

# ---------------------------------------------------------------------------
# Server, UI and app packaging
# ---------------------------------------------------------------------------
.PHONY: serve serve-lan serve-insecure apk bundle clean-apps

serve: ## Run the API + PWA server on 127.0.0.1 (this machine only)
	$(PY) -m src.server

serve-lan: ## Run the server on the LAN with an auto-generated token (for phones)
	$(PY) -m src.server --lan --print-token

serve-insecure: ## Run with no token at all (trusted network / Termux only)
	$(PY) -m src.server --host 0.0.0.0 --insecure

apk: ## Build the Android debug APK (needs Android SDK + gradle)
	cd apps/android && gradle :app:assembleDebug --console=plain

bundle: ## Build single-file binaries for this OS with PyInstaller
	$(PY) -m PyInstaller packaging/agent-hub.spec --noconfirm

icons: ## Regenerate the PWA/app PNG icons (stdlib-only, deterministic)
	$(PY) scripts/make_icons.py

clean-apps: ## Remove app build outputs
	rm -rf build dist apps/android/app/build apps/android/.gradle apps/ios/build
