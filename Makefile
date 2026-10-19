PORT ?= 8000
CONFIG ?= $(PWD)/config.yaml

DEV_IMAGE := psyb0t/mailboxd-dev:latest

PYPROJECT := pyproject.toml
BUMP_HOST := bash scripts/bump_exclude_newer.sh $(PYPROJECT)
GROUP ?=
TEST_ARGS ?=
UV_GROUP := $(if $(GROUP),$(if $(filter dev,$(GROUP)),--group dev,$(error GROUP must be empty or dev)),)

# Sandbox: everything dev-side runs INSIDE the dev image. The full env is
# baked into /opt/venv at image build time, so the host bind-mount stays
# clean (no .venv directory). Lockfile changes -> next `dev-image` build
# picks them up via docker layer cache invalidation on the COPY step.
UID := $(shell id -u)
GID := $(shell id -g)
DOCKER_SOCK := /var/run/docker.sock
DOCKER_GID := $(shell stat -c '%g' $(DOCKER_SOCK) 2>/dev/null || echo 0)

DEV_RUN := docker run --rm \
	-u $(UID):$(GID) \
	-e HOME=/tmp \
	-v $(PWD):/work \
	-w /work \
	$(DEV_IMAGE)

DEV_RUN_TTY := docker run --rm -it \
	-u $(UID):$(GID) \
	-e HOME=/tmp \
	-v $(PWD):/work \
	-w /work \
	$(DEV_IMAGE)

# Needs docker socket -- docker-in-docker -- for integration tests that
# spawn real containers (postal SMTP, dovecot IMAP, etc.).
DEV_RUN_DIND := docker run --rm \
	-u $(UID):$(GID) \
	--group-add $(DOCKER_GID) \
	-e HOME=/tmp \
	-e TESTCONTAINERS_RYUK_DISABLED=true \
	-e PYTHONPATH=$(PWD)/src \
	-v $(PWD):$(PWD) \
	-w $(PWD) \
	-v $(DOCKER_SOCK):$(DOCKER_SOCK) \
	$(DEV_IMAGE)

.PHONY: help dev-image shell version build audit audit-compose test-coverage watch \
        pkg-lock pkg-upgrade pkg-add pkg-remove pkg-update \
        run test test-unit test-docker lint format sec clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

version: ## Print the canonical package version
	@awk -F '"' '/^version = / {print $$2; exit}' pyproject.toml

build: ## Build the production image locally
	docker build -t psyb0t/mailbox:latest .

audit: dev-image ## Check the frozen runtime and dev dependency graph for advisories
	$(DEV_RUN) sh -ec 'uv lock --locked; uv export --frozen --all-groups --no-emit-project > /tmp/mailbox-requirements.txt; pip-audit --no-deps --disable-pip -r /tmp/mailbox-requirements.txt'

audit-compose: dev-image ## Validate production isolation, resource and health defaults
	$(DEV_RUN) python scripts/audit_compose.py

test-coverage: build dev-image ## Run the complete suite with an 85 percent coverage floor
	$(DEV_RUN_DIND) pytest --override-ini="addopts=-v --tb=short" --cov=mailboxd --cov-report=term-missing --cov-report=xml --cov-fail-under=85

watch: dev-image ## Run the watcher using the configured SQLite path and webhook targets
	$(DEV_RUN) python -m mailboxd.cli watch --config /work/config.yaml

# -----------------------------------------------------------------------------
# Dev container — every other target depends on this. Docker's layer cache
# makes rebuilds near-instant when pyproject + uv.lock haven't changed.
# -----------------------------------------------------------------------------

dev-image: ## Build/refresh the sandboxed dev image
	docker build -f Dockerfile.dev -t $(DEV_IMAGE) .

shell: dev-image ## Drop into a shell inside the dev container
	$(DEV_RUN_TTY) bash

# -----------------------------------------------------------------------------
# Lockfile mutations — only edit pyproject.toml + uv.lock on the bind-mount.
# Next `dev-image` build bakes the new deps. Every command bumps
# exclude-newer to today first so the supply-chain age gate is fresh.
# -----------------------------------------------------------------------------

pkg-lock: dev-image ## Refresh uv.lock (honors exclude-newer)
	$(DEV_RUN) uv lock

pkg-upgrade: dev-image ## Bump exclude-newer + refresh lock with newest pins
	$(BUMP_HOST)
	$(DEV_RUN) uv lock --upgrade

pkg-add: dev-image ## Add a package (usage: make pkg-add PKG=name[==ver] [GROUP=dev])
	@test -n "$(PKG)" || (echo "usage: make pkg-add PKG=name[==ver] [GROUP=dev]" >&2; exit 1)
	$(BUMP_HOST)
	$(DEV_RUN) uv add --no-sync $(UV_GROUP) $(PKG)

pkg-remove: dev-image ## Remove a package (usage: make pkg-remove PKG=name [GROUP=dev])
	@test -n "$(PKG)" || (echo "usage: make pkg-remove PKG=name [GROUP=dev]" >&2; exit 1)
	$(BUMP_HOST)
	$(DEV_RUN) uv remove --no-sync $(UV_GROUP) $(PKG)

pkg-update: dev-image ## Update a package (usage: make pkg-update PKG=name V=version [GROUP=dev])
	@test -n "$(PKG)" && test -n "$(V)" || (echo "usage: make pkg-update PKG=name V=version [GROUP=dev]" >&2; exit 1)
	$(BUMP_HOST)
	$(DEV_RUN) uv add --no-sync $(UV_GROUP) $(PKG)==$(V)

# -----------------------------------------------------------------------------
# Run / test / quality — `dev-image` dep means lockfile changes auto-apply.
# -----------------------------------------------------------------------------

run: dev-image ## Run HTTP server (mounts CONFIG=path/to/config.yaml)
	docker run --rm -it \
		-u $(UID):$(GID) -e HOME=/tmp \
		-v $(PWD):/work -w /work \
		-v $(CONFIG):/etc/mailboxd/config.yaml:ro \
		-e MAILBOXD_CONFIG=/etc/mailboxd/config.yaml \
		-p $(PORT):8000 \
		$(DEV_IMAGE) \
		uvicorn --factory mailboxd.server:create_app --host 0.0.0.0 --port 8000

test: build dev-image ## Run ALL tests, including the production container
	$(DEV_RUN_DIND) pytest --override-ini="addopts=-v --tb=short"

test-unit: dev-image ## Run only in-process unit tests
	$(DEV_RUN) pytest $(TEST_ARGS)

test-docker: build dev-image ## Run only the production container integration tests
	$(DEV_RUN_DIND) pytest -m docker --override-ini="addopts=-v --tb=short"

lint: dev-image ## Lint
	$(DEV_RUN) flake8 src tests scripts/audit_compose.py
	$(DEV_RUN) mypy src scripts/audit_compose.py

format: dev-image ## Format
	$(DEV_RUN) isort src tests scripts/audit_compose.py
	$(DEV_RUN) black src tests scripts/audit_compose.py

sec: dev-image ## Security scan (semgrep + bandit + pip-audit, parallel) -> sec.sarif for the Security tab; reports, never fails
	$(DEV_RUN) bash scripts/sec.sh

clean: ## Remove build / cache artifacts (host-side)
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .venv
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
