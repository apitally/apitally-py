# Litestar implementation notes

## Override `http.route` via plugin hooks

Litestar's stock `OpenTelemetryPlugin` writes `scope["path"]` (the raw request path) as `http.route`, not the parameterized template. Fix by overriding via the plugin's `server_request_hook_handler` and `client_response_hook_handler`. `scope["path_template"]` is populated by Litestar before `http.response.start` fires; stash the SERVER span on `scope` so the response hook can update it (the response hook receives the INTERNAL `http send` span, not the SERVER span).

```python
def _server_request_hook(span, scope):
    scope["__apitally_server_span"] = span

def _client_response_hook(span, scope, message):
    if message.get("type") != "http.response.start":
        return
    path_template = scope.get("path_template")
    server_span = scope.get("__apitally_server_span")
    if path_template and server_span is not None:
        method = scope.get("method", "")
        route = f"{method} {path_template}"
        server_span.set_attribute("http.route", route)
        server_span.update_name(route)

OpenTelemetryConfig(
    server_request_hook_handler=_server_request_hook,
    client_response_hook_handler=_client_response_hook,
    exclude_spans=["receive", "send"],
    tracer_provider=...,
    meter_provider=...,
)
```

`exclude_spans=["receive", "send"]` suppresses per-message INTERNAL spans (spec §6.6 forbids exporting them).

## Plugin must be passed at `Litestar()` construction

`Litestar.plugins` is built from a frozenset at construction time; there is no public late-registration API. `init_apitally(app: Litestar, ...)` cannot add the OTel plugin to an already-constructed app.

The user must pass our config helper at construction:

```python
app = Litestar(plugins=[apitally_litestar_plugin(...)], ...)
```

This is asymmetric to FastAPI/Flask/etc. where `init_apitally(app, ...)` mutates the app in place. Document in the user-facing setup docs.
