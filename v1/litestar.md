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

Because the mechanism is hook-free, it works identically when the user brought their own OTel setup (design.md §4's detection covers both patterns — our config is skipped, `before_send` still installs): the ContextVar-resolved write *repairs* the stock extractor's raw-path value on those spans. Lifespan sends never reach `before_send` (Litestar routes lifespan before the hook wrapping applies). For excluded requests the §3 map drops the span at end; the route write is local-only and harmless.

`exclude_spans=["receive", "send"]` suppresses per-message INTERNAL spans (spec §6.6 forbids exporting them).

## OTel config install and detection (Litestar 2.24 mechanics)

Litestar applies the stock OTel middleware specially: `_create_asgi_handler` fetches the plugin from the registry by class name (`plugins.get("OpenTelemetryPlugin")`) and wraps its middleware around the whole app — exception handler → CORS → OTel, outermost. Regular `middleware`-list entries run at route level, structurally inside it. So exactly one plugin's middleware is ever applied at app level, and our transport middleware (a regular middleware entry) always runs inside the SERVER span with no ordering work — nested SERVER spans are structurally impossible.

`ApitallyPlugin.on_app_init` therefore:

- **Detects user instrumentation** as either a stock `OpenTelemetryPlugin` instance in `app_config.plugins` or an OTel `DefineMiddleware` in `app_config.middleware` (the documented pre-2.22 pattern; Litestar's `_patch_opentelemetry_middleware` hoists it into a synthetic plugin after all plugin inits — last one wins if multiple). Either found → skip installing our config; the user's middleware carries the SERVER span, `before_send` repairs `http.route`, and the §3 backstop drops their unexcluded receive/send spans.
- **Installs ours otherwise** by appending a stock `OpenTelemetryPlugin(our_config)` to `app_config.plugins` (via reassignment): the registry picks it up and applies it at app level, and the hoist workaround stands down because a plugin is present. Installing via the middleware list instead would ride the hoist path — labeled a workaround in Litestar's own source — whose last-one-wins pop silently discards one config when a legacy raw middleware coexists.
- **Appends the transport middleware** to `app_config.middleware` in both cases (route level = inside the SERVER span).

## Activation trigger

`ApitallyPlugin.on_app_init` appends the activation trigger to `app_config.on_startup`; the hook runs during lifespan startup, before the server serves — the design.md §7 lifespan trigger's equivalent. There is no shim and no first-request fallback: the handler chain is frozen at the end of `Litestar.__init__` and the transport middleware runs inside the SERVER span, so no pre-span first-request seam exists. Lifespan-disabled deployments are unsupported SDK-wide (design.md §7).

## Plugin must be passed at `Litestar()` construction

`Litestar.plugins` is built from a frozenset at construction time; there is no public late-registration API, and the ASGI handler chain (where the OTel middleware gets baked in) is frozen at the end of `Litestar.__init__` as well. There is no `init_apitally(app: Litestar, ...)`.

The user passes the plugin at construction:

```python
app = Litestar(plugins=[ApitallyPlugin(...)], ...)
```

`ApitallyPlugin` takes the same kwargs as `init_apitally` and runs the same configure/activate path from its `on_app_init` hook, going through the §8 config singleton, so the first-call-wins re-call semantics match the other frameworks. This is asymmetric to FastAPI/Flask/etc. where `init_apitally(app, ...)` mutates the app in place. Document in the user-facing setup docs.
