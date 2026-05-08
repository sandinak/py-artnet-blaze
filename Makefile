# py-artnet-blaze — local dev tasks.
#
#   make             create venv, install dev deps, run tests
#   make venv        create .venv only
#   make install     install runtime deps into .venv
#   make install-dev install runtime + test deps into .venv
#   make test        run pytest with coverage
#   make coverage    same as test, plus html report at htmlcov/index.html
#   make run         run the daemon against config.yaml.example
#   make lint        sanity-check that the package imports cleanly
#   make clean       remove .venv and caches

PYTHON       ?= python3
VENV         := .venv
VENV_PY      := $(VENV)/bin/python
VENV_PIP     := $(VENV)/bin/pip
VENV_PYTEST  := $(VENV)/bin/pytest

# Sentinel files: rebuild deps only when the requirements files change.
VENV_STAMP        := $(VENV)/.venv-stamp
INSTALL_STAMP     := $(VENV)/.install-stamp
INSTALL_DEV_STAMP := $(VENV)/.install-dev-stamp

.PHONY: all venv install install-dev test coverage run lint clean help

all: install-dev test

help:
	@awk 'BEGIN{FS=":.*?##"} /^[a-zA-Z_-]+:.*?##/ {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

venv: $(VENV_STAMP) ## Create the virtualenv

$(VENV_STAMP):
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --quiet --upgrade pip
	@touch $(VENV_STAMP)

install: $(INSTALL_STAMP) ## Install runtime dependencies

$(INSTALL_STAMP): $(VENV_STAMP) requirements.txt
	$(VENV_PIP) install --quiet -r requirements.txt
	@touch $(INSTALL_STAMP)

install-dev: $(INSTALL_DEV_STAMP) ## Install runtime + test dependencies

$(INSTALL_DEV_STAMP): $(VENV_STAMP) requirements.txt requirements-dev.txt
	$(VENV_PIP) install --quiet -r requirements-dev.txt
	@touch $(INSTALL_DEV_STAMP)

test: install-dev ## Run pytest with coverage (fails under 85%)
	$(VENV_PYTEST)

coverage: install-dev ## Run pytest and generate HTML coverage report
	$(VENV_PYTEST) --cov-report=html
	@echo "→ open htmlcov/index.html"

lint: install ## Smoke-check the package imports cleanly
	$(VENV_PY) -c "import artnet_blaze; from artnet_blaze import main, dmx, poe, artnet, sink, config; print('ok', artnet_blaze.__version__)"

run: install ## Run the daemon locally with the example config
	$(VENV_PY) -m artnet_blaze -c config.yaml.example

clean: ## Remove venv, coverage artifacts, and caches
	rm -rf $(VENV) htmlcov .coverage .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
