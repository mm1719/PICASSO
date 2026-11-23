PYTHON ?= python3

.PHONY: test-portable web-lint web-build web-unit check

# Exported hermetic tests, including a temporary Unix socket. No hardware, sudo or Docker.
test-portable:
	PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. $(PYTHON) -m pytest -q tests/unit tests/whitebox tests/integration

web-lint:
	npm --prefix apps/web run lint

web-build:
	npm --prefix apps/web run build

web-unit:
	npm --prefix apps/web run test:unit

check: test-portable web-lint web-build web-unit
