## Naming and wording conventions

### General rules

- Use plain, precise English. No invented shorthand, metaphors, or informal jargon (past offenders: "wire final", "hoist path", "smuggle", "plumbing", "canary").
- A word qualifies only by referring to an actual thing in this codebase or its dependencies, never by sounding technical: "deferred export" (the `defer_export`/`finish_export` methods), "SERVER span" (OTel `SpanKind.SERVER`), "transport middleware" (the `Apitally*Middleware` classes).
- Prefer a longer clear name over a compact clever one.
- Vague verbs need an object or a from/to: not `resolve` but `resolve_value`.

### Tests

- A test name states the observable behavior it pins, readable without the test body: `test_no_response_size_when_client_stops_reading_mid_stream`, `test_request_body_not_read_for_disallowed_content_type`, `test_span_export_waits_for_streaming_response_to_complete`.
- Name the behavior, not the mechanism or an internal codename.
- Tests pinning the same scenario in different framework integrations share the same name.

### Functions and methods

- Boolean predicates read as questions: `is_`/`should_` prefixes (`should_skip_activation`, `is_sampled_in`, `is_not_self_log`). Never name a predicate as an imperative command.
- The name states what the function actually does, including its outcome: a function that only logs a warning is `warn_if_sampler_drops_spans`, not `check_sampler`. A method that may discard or buffer as well as export is `process_ended_span`, not `export_span`.
- One concept, one name across modules: the content-type allowlist check is `is_allowed_content_type` everywhere. Names align with the config option they implement (`_get_django_view_paths` for `include_django_views`).
- When renaming a function, rename its associated constants to match.
- Public API names (`init_apitally`, `set_consumer`, `capture_exception`, `instrument_*`) are stable; naming improvements are internal only.

## Comments and docstrings

- A comment states something the code cannot: a constraint, an external system's behavior, or the reason for a choice. Name the real component (the env var, the thread, the framework version behavior), never a metaphor.
- No historical references: nothing about the 0.x SDK, "previously", or "ported from". Comments describe the present code only.
- A comment must sit next to the code it justifies and stay accurate about what that code covers.
