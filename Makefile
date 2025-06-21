.PHONY: format check test test-coverage

format:
	uv run ruff check apitally tests --fix --select I
	uv run ruff format apitally tests

check:
	uv run ruff check apitally tests
	uv run ruff format --diff apitally tests
	uv run mypy --install-types --non-interactive apitally tests

test:
	uv run pytest -v --tb=short

test-coverage:
	uv run pytest -v --tb=short --cov --cov-report=xml
