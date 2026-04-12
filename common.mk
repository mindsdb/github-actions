# Common Makefile fragment for MindsDB Python projects
# Include with: -include .make/common.mk
#
# Repos must define these before including:
#   MODULE          - The Python module name to lint/test (e.g., 'auth', 'minds')
#   TEST_PATHS      - Path to test directory (e.g., 'tests/unit/')
#   COVERAGE_THRESHOLD - Minimum coverage percentage (e.g., '80' or '100')
#
# Optional overrides:
#   PYTHON_VERSION  - Python version for CI (default: 3.12)
#   VENV            - Virtual environment path (default: env)
#   REQUIREMENTS_FILE - Requirements file for CI (default: requirements/requirements.txt)

.PHONY: help activate deps lint check/lint check/format format check/fix test/unit test/unit/coverage test/unit/fast test/report docker/build docker/run docker/stop docker/clean

# --- Configuration ---
SHELL := /bin/bash
.ONESHELL:

# Windows detection
ifeq ($(OS),Windows_NT)
    VENV_DIR = Scripts
    PYTHON_EXE = python.exe
    SET_ENV = set
else
    VENV_DIR = bin
    PYTHON_EXE = python
    SET_ENV = export
endif

# Defaults (repos can override before including)
PYTHON_VERSION ?= 3.12
VENV ?= env
REQUIREMENTS_FILE ?= requirements/requirements.txt
IN_CONTAINER := $(shell (test -f /.dockerenv || test -n "$$KUBERNETES_SERVICE_HOST") && echo 1 || echo 0)
DOCKER_COMMAND := $(shell if docker compose version >/dev/null 2>&1; then echo "docker compose"; elif docker-compose version >/dev/null 2>&1; then echo "docker-compose"; fi)

# Python executable selection
ifeq ($(IN_CONTAINER),1)
  PYTHON ?= python
else
  PYTHON ?= $(VENV)/$(VENV_DIR)/$(PYTHON_EXE)
endif

# --- Help Target ---
help: ## Display this help message
	@echo "Usage: make [target]"
	@echo ""
	@echo "Available targets:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_\/-]+:.*?## / {printf "  \033[36m%-25s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- Virtual Environment Targets ---
ifeq ($(IN_CONTAINER),1)
activate: ## No-op inside container (packages already installed)
	@echo "Running inside container - using system Python with pre-installed packages."

clean/venv: ## Remove virtual environment (no-op in container)
	@echo "No venv to clean inside container."

reinstall: ## No-op inside container
	@echo "No venv to reinstall inside container."
else
$(VENV)/$(VENV_DIR)/activate: $(REQUIREMENTS_FILE) ## Create virtualenv and install dependencies
	@echo "Creating virtual environment at $(VENV)..."
	uv venv "$(VENV)" --allow-existing --python $(PYTHON_VERSION)
	@echo "Virtual environment created. Installing requirements..."
	uv pip install --python "$(VENV)/$(VENV_DIR)/$(PYTHON_EXE)" -r $(REQUIREMENTS_FILE)
	@echo "Virtual environment setup complete."

activate: $(VENV)/$(VENV_DIR)/activate ## Activate the virtual environment

clean/venv: ## Remove virtual environment
	rm -rf $(VENV)

reinstall: clean/venv activate ## Clean and reinstall everything
endif

# --- Docker Compose Check ---
deps: ## Check docker compose is available
ifndef DOCKER_COMMAND
	@echo "Docker compose not found. Please install either docker-compose (the tool) or the docker compose plugin."
	exit 1
endif

# --- Linting and Formatting Targets ---
lint: check/lint check/format ## Run all linting and formatting checks

check/lint: activate ## Check code style with Ruff
	$(PYTHON) -m ruff check $(MODULE)

check/format: activate ## Check code formatting with Ruff
	$(PYTHON) -m ruff format $(MODULE) --check

format: activate ## Format code with Ruff
	$(PYTHON) -m ruff format $(MODULE)

check/fix: activate ## Fix linting issues with Ruff
	$(PYTHON) -m ruff check $(MODULE) --fix

# --- Testing Targets ---
test/unit: activate ## Run unit tests
	$(PYTHON) -m pytest $(TEST_PATHS)

test/unit/coverage: activate ## Run unit tests with coverage
	$(PYTHON) -m pytest --cov=$(MODULE) $(TEST_PATHS) --cov-fail-under=$(COVERAGE_THRESHOLD)

test/unit/fast: activate ## Run only tests affected by changed files (pytest-testmon)
	$(PYTHON) -m pytest --testmon $(TEST_PATHS) -p no:randomly --no-header -q

test/report: activate ## Generate HTML coverage report
	$(PYTHON) -m pytest --cov=$(MODULE) $(TEST_PATHS) --cov-report html

# --- Docker Targets ---
docker/build: deps ## Build the docker images
	$(SET_ENV) DOCKER_BUILDKIT=1 && $(DOCKER_COMMAND) build

docker/run: deps ## Run the full application in Docker
	$(SET_ENV) DOCKER_BUILDKIT=1 && $(DOCKER_COMMAND) up

docker/stop: ## Stop the docker containers
	$(DOCKER_COMMAND) down

docker/clean: ## Stop containers and remove volumes
	$(DOCKER_COMMAND) down -v
