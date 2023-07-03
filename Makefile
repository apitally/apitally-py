.PHONY: format check test

format:
	ruff check starlette_apitally tests --fix --select I
	black starlette_apitally tests

check:
	ruff check starlette_apitally tests
	mypy --install-types --non-interactive starlette_apitally tests
	black --check --diff starlette_apitally tests
	poetry check

test:
	pytest -v --tb=short
