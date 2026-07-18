# Agent guidance

## Scope

- The SDK does not support forking after Apitally has activated. Pre-fork servers configure in the parent and activate in each worker after fork; spawning children from an activated process is outside the supported lifecycle.

## Code style

- Write the least amount of code that gets the job done.
- Write modern, well-structured, maintainable Python within the supported range of 3.10-3.14: use the syntax 3.10 allows (`X | Y` unions, `match`, parenthesized context managers), nothing that requires 3.11+ at runtime.
- Imports go at the top of the module. A function-body import needs a good reason (circular-import break, optional dependency, deferred heavy import), and even then imports are grouped at the top of the function body, never in the middle of other code.
- Underscore prefixes only where genuinely required to separate public from private API (user-facing modules). Internal modules do not prefix every variable and function.
- Function order within a module is deliberate, not accidental: public entry points first, helpers after, ordered so the module reads top-down.
- No single-use helper functions unless extraction meaningfully improves readability at the call site.

## Naming and wording conventions

- Use plain, precise English. No invented shorthand, metaphors, or informal jargon (past offenders: "wire final", "hoist path", "smuggle", "plumbing", "canary").
- A word qualifies only by referring to an actual thing in this codebase or its dependencies, never by sounding technical: "deferred export" (the `defer_export`/`finish_export` methods), "SERVER span" (OTel `SpanKind.SERVER`), "transport middleware" (the `Apitally*Middleware` classes).
- Prefer a longer clear name over a compact clever one.
- Vague verbs need an object or a from/to: not `resolve` but `resolve_value`.
- Boolean predicates read as questions: `is_`/`should_` prefixes (`should_skip_activation`, `is_sampled_in`, `is_not_self_log`). Never name a predicate as an imperative command.
- The name states what the function actually does, including its outcome: a function that only logs a warning is `warn_if_sampler_drops_spans`, not `check_sampler`. A method that may discard or buffer as well as export is `process_ended_span`, not `export_span`.
- One concept, one name across modules: the content-type allowlist check is `is_allowed_content_type` everywhere. Names align with the config option they implement (`_get_django_view_paths` for `include_django_views`).
- When renaming a function, rename its associated constants to match.
- Public API names (`init`, `set_consumer`, `capture_exception`, `instrument_*`) are stable; naming improvements are internal only.

## Comments and docstrings

- Comments are sparse and concise (one or two lines). A comment states something the code cannot: a constraint, an external system's behavior, or the reason for a choice. It explains the WHY, never narrates the WHAT; a comment that restates the code below it does not get written.
- Name the real component (the env var, the thread, the framework version behavior), never a metaphor.
- No historical references: nothing about the 0.x SDK, "previously", or "ported from". Comments describe the present code only.
- A comment must sit next to the code it justifies and stay accurate about what that code covers.

## Checks

- Verify changes with the Makefile targets, never with hand-picked subsets of them: `uv run make check` (ruff lint, format diff, ty on both `apitally` and `tests`, `uv lock --locked`) and `uv run make test`. Running `ty check apitally` alone misses diagnostics in `tests/`; CI runs the full targets, so only their output counts as green.

## Testing

- Never replace Apitally's own classes or functions with mocks. Mocking is only acceptable where the test would otherwise leave the process (the network, `os.fork` where forking in a test is impractical).
- One focused test module per shared module, one integration module per framework driving a small real app, shared fixtures in `tests/conftest.py`. Test files are named after the module they test, never after scenarios.
- Every test needs an important reason to exist: it pins a spec MUST, a settled design decision, or a behavior a plausible change would silently break. Tests that restate the implementation, or assert theoretical edge cases no real deployment hits, do not get written.
- Prefer one integration test proving a flow end-to-end over several micro-tests asserting its intermediate steps.
- A test name states the observable behavior it pins, readable without the test body: `test_no_response_size_when_client_stops_reading_mid_stream`, `test_request_body_not_read_for_disallowed_content_type`, `test_span_export_waits_for_streaming_response_to_complete`. Name the behavior, not the mechanism or an internal codename.
- Tests pinning the same scenario in different framework integrations share the same name.
- The shared autouse fixture in `tests/conftest.py` isolates process-global OTel state between tests (config singleton, tracer-provider global, root-logger handler, instrumentor singletons, semconv env var). Rely on it instead of resetting state in individual tests.
