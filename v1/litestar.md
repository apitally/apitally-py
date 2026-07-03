# Litestar implementation notes

## Override `http.route` via `before_send`

Litestar's stock `OpenTelemetryPlugin` writes a method-prefixed raw path (`"GET /users/123"`) as `http.route`, not the parameterized template — doubly violating spec §6.1. The ASGI instrumentation's `client_response_hook` cannot fix this: it only fires inside send-span creation, which `exclude_spans=["receive", "send"]` (required per spec §6.6) suppresses. Instead, `ApitallyPlugin` registers a Litestar `before_send` lifecycle hook in `on_app_init`. `scope["path_template"]` is populated by Litestar before `http.response.start` is sent (verified against Litestar 2.24); the SERVER span comes from the design.md §5 ContextVar — set synchronously at span start in the request task's context, so it is visible in `before_send`, with no instrumentor hook involved (design.md §4 forbids hook-dependent glue).

```python
async def _before_send(message, scope):
    if message.get("type") != "http.response.start":
        return
    path_template = scope.get("path_template")
    server_span = _server_span_var.get(None)  # the §5 ContextVar
    if path_template and server_span is not None:
        server_span.set_attribute("http.route", str(path_template))
        method = scope.get("method", "")
        server_span.update_name(f"{method} {path_template}")

OpenTelemetryConfig(
    exclude_spans=["receive", "send"],
    tracer_provider=...,
)
# ApitallyPlugin.on_app_init registers _before_send on the app and installs the OTel config
```

Note `http.route` gets the bare template (semconv); the method prefix belongs only in the span name.

Because the mechanism is hook-free, it works identically when the user already registered Litestar's stock `OpenTelemetryPlugin` (design.md §4's detection list covers this case — our config is skipped, `before_send` still installs): the ContextVar-resolved write *repairs* the stock extractor's raw-path value on those spans. Lifespan sends never reach `before_send` (Litestar routes lifespan before the hook wrapping applies). For excluded requests the §3 map drops the span at end; the route write is local-only and harmless.

`exclude_spans=["receive", "send"]` suppresses per-message INTERNAL spans (spec §6.6 forbids exporting them).

## Plugin must be passed at `Litestar()` construction

`Litestar.plugins` is built from a frozenset at construction time; there is no public late-registration API, and the ASGI handler chain (where the OTel middleware gets baked in) is frozen at the end of `Litestar.__init__` as well. There is no `init_apitally(app: Litestar, ...)`.

The user passes the plugin at construction:

```python
app = Litestar(plugins=[ApitallyPlugin(...)], ...)
```

`ApitallyPlugin` takes the same kwargs as `init_apitally` and runs the same configure/activate path from its `on_app_init` hook, going through the §8 config singleton, so idempotency and re-call semantics match the other frameworks. This is asymmetric to FastAPI/Flask/etc. where `init_apitally(app, ...)` mutates the app in place. Document in the user-facing setup docs.
