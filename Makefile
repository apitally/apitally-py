.PHONY: format check test test-coverage

format:
	ruff check apitally tests --fix --select I
	black apitally tests

check:
	ruff check apitally tests
	mypy --install-types --non-interactive apitally tests
	black --check --diff apitally tests
	poetry check

test:
	pytest -v --tb=short

test-coverage:
	pytest -v --tb=short --cov --cov-report=xml
