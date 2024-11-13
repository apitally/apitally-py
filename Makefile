.PHONY: format check test test-coverage

format:
	ruff check apitally tests --fix --select I
	ruff format apitally tests

check:
	ruff check apitally tests
	ruff format --diff apitally tests
	mypy --install-types --non-interactive apitally tests

test:
	pytest -v --tb=short

test-coverage:
	pytest -v --tb=short --cov --cov-report=xml
