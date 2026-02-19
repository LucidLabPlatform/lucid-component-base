PYTHON ?= python3
VENV ?= .venv

.PHONY: help setup-venv test test-unit test-coverage build clean

help:
	@echo "LUCID Component Base (SDK)"
	@echo "  make setup-venv      - Create .venv, install project + deps"
	@echo "  make test            - Run tests"
	@echo "  make test-unit       - Unit tests only"
	@echo "  make test-coverage   - Tests with coverage report"
	@echo "  make build           - Build wheel and sdist (run make setup-venv first)"
	@echo "  make clean           - Remove build artifacts"

setup-venv:
	@test -d $(VENV) || ($(PYTHON) -m venv $(VENV) && echo "Created $(VENV).")
	@$(VENV)/bin/pip install -q -e .
	@$(VENV)/bin/pip install -q build pytest pytest-cov
	@echo "Ready. Run 'make test' or 'make build'."

test: test-unit
	@echo "All tests passed."

test-unit:
	@if [ -d tests ]; then \
		pytest tests/ -v -q; \
	else \
		echo "No tests directory found."; \
	fi

test-coverage:
	@if [ -d tests ]; then \
		pytest tests/ --cov=src/lucid_component_base --cov-report=html --cov-report=term-missing -q; \
	else \
		echo "No tests directory found."; \
	fi

build:
	@test -d $(VENV) || (echo "Run 'make setup-venv' first." && exit 1)
	@$(VENV)/bin/python -m build

clean:
	@rm -rf build/ dist/ *.egg-info src/*.egg-info .venv
