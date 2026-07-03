# v1 design docs — round-2 review findings

Multi-persona review of `design.md` (primary) with `spec.md` as the authoritative read-only contract and `litestar.md` / `wsgi.md` as appendices. Round 2, after all round-1 decisions were applied (see `review.md` for the round-1 audit trail). Reviewers: coherence, feasibility, security-lens, product-lens, adversarial. Date: 2026-07-03.

**15 items (5 errors, 10 omissions). 1 FYI observation. No fixes auto-applied.**

The headline: round 2 found no problems with the decisions — it found gaps between the decisions and the mechanics that deliver them. Three P1 errors are verified-at-certainty implementation falsehoods in §4/§6, and the semconv finding is a spec-MUST violation as written.

## P1 — errors

### 1. Flask response-body capture writes to an already-ended span — confidence 100 (feasibility)

**Section:** §6 Body and header capture

The Flask instrumentor (0.64b0) starts the SERVER span in `before_request` and ends it via `teardown_request`, which Flask runs inside `wsgi_app`'s `finally` block before the WSGI iterable is ever returned upward. Every WSGI layer — inside or outside the instrumentor's wrapper, so the §6 ordering rule does not help — accumulates response chunks strictly after span end, and the `apitally.response.body` write is silently dropped. §6's justification sentence ("the SERVER span ends when the instrumentor's layer completes the response") is true for ASGI and Django but false for Flask. Request bodies and response headers have a live-span window (the wrapped `start_response` fires while the context is still pushed); the response body does not exist at any point during the span's lifetime at WSGI level.

**Fix:** Scope the WSGI transport middleware's span writes for Flask: request body and response headers at the wrapped `start_response`; response-body capture moves to Flask framework glue — an `after_request` hook that reads `response.get_data()` when `direct_passthrough` is false and writes the attribute while the span is alive (teardown runs after `after_request`). State that streaming/direct-passthrough Flask responses are not captured. Correct §6's ordering-rule sentence. [REVISIT] — VERIFIED (2026-07-03): HOLDS on every claim, and worse — the request-body write at middleware entry is also broken (span doesn't exist yet; reused worker threads hold the previous request's ended span), and each dropped write logs an SDK warning per request. → DECIDED (grilling 3): split write sites per the proposed fix — request body + response headers at wrapped `start_response` (wire-final headers), response body via `after_request` with the `direct_passthrough` guard; streaming not captured; §6 Flask clause re-justified (attach-first is for `wsgi.input` replacement, not span lifetime). Applied to §6.

### 2. Starlette instrumentor does not wrap the built middleware stack; ordering is not "automatic" — confidence 100 (feasibility, adversarial)

**Section:** §6 Body and header capture / §4

Only FastAPI's instrumentor monkeypatches `build_middleware_stack` and wraps the whole built stack. `StarletteInstrumentor.instrument_app` (0.64b0) calls `app.add_middleware(OpenTelemetryMiddleware, ...)`, and Starlette's `add_middleware` inserts at `user_middleware[0]` — last added is outermost. Following §7's configure order (instrument, then attach ours), the transport middleware lands OUTSIDE the SERVER span on every Starlette app: body and header attributes are set after span end and silently dropped.

**Fix:** Split the §6 per-framework rule: FastAPI — automatic (stack-wrap patch); Starlette — attach our transport middleware BEFORE calling `instrument_app` (same rule as Flask). [APPROVED] — RE-VERIFIED (2026-07-03): HOLDS at certainty with file:line evidence; purely a documentation-ordering fix since `init_apitally` makes both calls. → DECIDED (grilling 3): applied to §6 (per-framework rule split) and §7 (configure list order: attach transport middleware, then instrumentor).

### 3. "Nothing degrades" is false for pre-instrumented Flask/Starlette — confidence 75 (adversarial)

**Section:** §4 Already-instrumented frameworks / §6

In the already-instrumented scenario the user instrumented first (e.g. `opentelemetry-instrument` patches app classes at construction), so "attach ours before instrumenting" is impossible: our `wsgi_app` wrap (Flask) or `add_middleware` insert-at-0 (Starlette) necessarily ends up OUTSIDE the OTel layer, and body attributes land after span end. Body capture degrades for exactly the cooperative auto-instrumentation segment — falsifying §4's "nothing degrades" and, with it, the premise for silent DEBUG-only adaptation when body capture is enabled (§9 reserves WARNING for actionable data loss, which this then is).

**Fix (manual):** Scope the "nothing degrades" claim to span/log/histogram flow and handle the transport-middleware position explicitly: Starlette — insert our middleware into `app.user_middleware` immediately after the existing `OpenTelemetryMiddleware` entry (inside it); Flask — the instrumentor's closure-based wrap cannot be entered, so document body capture as unavailable when the app was instrumented first, and decide whether body-capture-enabled qualifies as the §9 actionable-data-loss WARNING case. [REVISIT] — VERIFIED (2026-07-03): HOLDS-MODIFIED. FastAPI is NOT affected (lazy stack-wrap keeps later-added middleware inside); Flask is worse than stated — §6's WSGI mechanism was broken for response bodies in every ordering, so the finding-1 fix (hook-timed writes) covers pre-instrumented Flask for free. → DECIDED (grilling 3): Starlette targeted `user_middleware` insert after the existing `OpenTelemetryMiddleware` entry (~3 lines, in-tree precedent in `uninstrument_app`); Flask/FastAPI/Litestar need nothing; "nothing degrades" stays true, silent adaptation stands, no §9 WARNING. Applied to §4.

## P1 — omissions

### 4. Semconv stability mode is undecided; stock instrumentors emit old semconv by default — confidence 75 (feasibility)

**Section:** §4 / §5

spec §6.1 is a MUST ("The SDK MUST emit stable HTTP semconv"), and §5's `exclude_on_request` contract names `url.path`, `http.request.method`, `user_agent.original` — but contrib 0.64b0 emits OLD semconv (`http.method`, `http.target`, `http.user_agent`) unless `OTEL_SEMCONV_STABILITY_OPT_IN` includes `http`, a process-global env var read exactly once. Built as written, the SDK violates the spec MUST, the documented callback attribute names are absent from spans, and attributes with no server-side fallback (`http.request.body.size` / `http.response.body.size`) silently vanish. The decision has cooperative-mode consequences: flipping the process-global opt-in changes the attribute names the user's own backend receives from all their HTTP instrumentations.

**Fix (manual):** Add a semconv decision to §4: own-it-all — set `OTEL_SEMCONV_STABILITY_OPT_IN=http` (only when unset) at configure time, before any instrumentor initializes; cooperative — leave the user's environment untouched and have the `ApitallySpanProcessor` on_end rewrite (§6) normalize old-name attributes to stable names on the Apitally-bound span copy, documenting that user callbacks may see old names in cooperative mode. [REVISIT] — VERIFIED (2026-07-03): HOLDS-MODIFIED. Mechanism confirmed (old-by-default, process-global once-latch at first instrumentor init), but two impact claims overstated: no ingest data loss (spec §6.1 server-side old-name fallbacks cover every contrib-sourced attribute) and body sizes never come from instrumentors in any mode (finding 5). → DECIDED (grilling 3): one unconditional line at the top of configure — set `OTEL_SEMCONV_STABILITY_OPT_IN=http/dup` when unset (both name sets: stable for Apitally, old preserved for cooperative backends); respect a user-set value; the on_end normalization layer is CUT (overspecification — protects a case the fallbacks already cover). Already-latched cooperative residual documented; contract test pins query redaction on old-mode spans (`http.target` embeds the query string). Applied to §4 (new "Semantic conventions" subsection) and §7 configure list.

### 5. `http.request.body.size` / `http.response.body.size` have no owner — confidence 75 (adversarial)

**Section:** §4 / §6

"The stock instrumentor produces the SERVER span and sets standard HTTP attributes" is false for the spec §6.1 body-size pair: opentelemetry-instrumentation-asgi 0.64b0 records content lengths only as histogram metrics on the instrumentor's meter (which §2 deliberately withholds) — `collect_request_attributes` sets no size attribute on the span. spec §6.1 requires both "set regardless of body capture," and no design component is assigned; every v1 request log ships without sizes.

**Fix:** Assign both attributes to the §6 transport middleware: `http.request.body.size` from Content-Length (or accumulated request bytes on ASGI), `http.response.body.size` from accumulated response bytes, on the SERVER span via the §5 ContextVar, independent of the body-capture toggles. — VERIFIED (2026-07-03): HOLDS, stronger than filed (WSGI/Django instrumentors have zero size handling, not even metrics). → DECIDED (grilling 3): 0.x-parity semantics instead of the proposed accumulate-on-ASGI (which would wrap `receive` with capture off and undercount unread bodies): request size from Content-Length on both transports with capture backfill when free; response size from response-start Content-Length else a running counter (ASGI), wire-final `start_response` Content-Length (Flask), header or `len(response.content)` (Django). Same values feed the §7.1 histograms. Upstream spec task: soften §6.1 "regardless of body capture" to "when the size is determinable". Applied to §4 and §6 (new "Body size attributes" subsection).

### 6. `APITALLY_OTLP_ENDPOINT` override has no scheme/destination validation — confidence 75 (security-lens)

**Section:** §14 Configuration loading

The write token (a bearer credential per spec §3) and any captured bodies/headers/consumer PII are sent to whatever host the env var resolves to, with no check that it is a well-formed `https://` URL. A misconfigured or typo'd value silently downgrades transport to plaintext or redirects credential plus payload to an arbitrary host. Distinct from the rejected F1 (a UX warning): this is a missing correctness/safety gate on the destination, not a log line. The 0.x precedent (`HUB_BASE_URL`) has the same gap; v1 carries it into a context where the payload includes bodies/headers instead of a UUID.

**Fix:** At activation, when `APITALLY_OTLP_ENDPOINT` is set, treat a value that doesn't parse as an `https://` URL as a §9 setup error (log via the setup-error path; don't construct the exporter with it). [REJECT]

## P2 — errors

### 7. Keep-root-only ContextVar breaks spec §6.8 metrics counting for excluded BlackSheep requests — confidence 75 (adversarial)

**Section:** §5 / §4 / §3

The ContextVar is set only when on_start classifies a span as a KEEP-root SERVER span, but BlackSheep's `http.route` is written through that same ContextVar by the router wrap. For an excluded request (kube-probe hitting a registered /healthz route), the span enters the map as `(False, None)`, the var stays empty, the router wrap cannot set `http.route` — and the glue's histogram observation has an empty route, which spec §7.1 says MUST NOT be recorded. Excluded BlackSheep requests silently vanish from request metrics, contradicting §3's own statement (and spec §6.8) that excluded requests are still counted.

**Fix:** Set the ContextVar for every local-root SERVER span at on_start (excluded and OPTIONS spans included), keeping exclusion enforcement exclusively in the §3 map; writes to excluded spans are harmless because the span is dropped at on_end. Also removes the stale-handle window for excluded requests in reused WSGI thread contexts. [APPROVED] — RE-VERIFIED (2026-07-03): HOLDS; the fix strictly removes a condition, no downsides found (exclusion guarantee untouched, OPTIONS gated by method in the glue, log stamping uses the map not the var). → DECIDED (grilling 3): applied to §5.

### 8. litestar.md route glue still lives in an instrumentor hook, contradicting §4's hook-independence — confidence 75 (adversarial)

**Section:** litestar.md — Override `http.route` via `before_send`

§4 declares all glue lives in Apitally-owned paths "never in instrumentor hooks," yet litestar.md's `before_send` mechanism depends on `server_request_hook_handler` stashing the span on the scope. A Litestar user who already registered Litestar's stock OpenTelemetryPlugin (a case §4's detection list omits) never gets our hook installed; `before_send` finds no stashed span and `http.route` stays the raw path — violating spec §6.1's route-template MUST. The §5 ContextVar already provides the SERVER span handle inside `before_send`.

**Fix:** Rewrite litestar.md's mechanism to resolve the SERVER span in `_before_send` via the §5 ContextVar and drop `server_request_hook_handler` from the OpenTelemetryConfig. [REVISIT] — VERIFIED (2026-07-03): HOLDS, and stronger — the stock plugin's default extractor sets `http.route` to a method-prefixed raw path (doubly spec-violating), which the ContextVar-based `before_send` actively repairs in the pre-instrumented case; context flow traced through Litestar 2.24 (var set at span start is visible in `before_send`; lifespan never reaches it). → DECIDED (grilling 3): rewrite adopted; stock `OpenTelemetryPlugin` added to §4's already-instrumented detection list. Applied to litestar.md and §4.

## P2 — omissions

### 9. Outermost activation shim is unachievable via middleware on FastAPI — confidence 100 (feasibility, adversarial; promoted via cross-persona agreement)

**Section:** §7 Activate phase

The first-request guarantee depends on the shim sitting outside the instrumentor's middleware, but FastAPI's instrumentor patches `build_middleware_stack` and wraps the ENTIRE built stack — anything added through `add_middleware` lands inside the OTel layer. With lifespan disabled, the SERVER span is created against the unresolved ProxyTracer before the shim activates and the first request is swallowed — the exact failure the R8 guarantee was decided to prevent, silently violated by the obvious implementation.

**Fix:** State the shim's attachment per framework in §7: FastAPI — chain-patch `app.build_middleware_stack` after the instrumentor's own patch, wrapping the returned stack in the activation shim (the stack builds lazily on first call, so the patch lands in time); Starlette — add the shim via `add_middleware` AFTER `instrument_app` (last-added is outermost); Flask — unchanged (wrap `wsgi_app` after the instrumentor). Note explicitly that `add_middleware` cannot reach the outermost position on FastAPI. [REVISIT] — VERIFIED (2026-07-03): HOLDS at certainty; all softer alternatives ruled out (instance `__call__` assignment ignored by type-based lookup; wrapper-return breaks `uvicorn main:app`; no ProxyTracer-resolution hook). Grilled on population honesty: lifespan-disabled FastAPI is rare (explicit `--lifespan off`), but the shim needs an attachment point on FastAPI regardless — the chain-patch is choosing the correct one of two, ~5 marginal lines. → DECIDED (grilling 3): adopted, with the one implementer-facing condition written down (our chain-patch applies after our own instrument call; last patcher is outermost). Applied to §7.

### 10. Cooperative sampler warning predicate undefined; the OTel default sampler is the ambiguous case — confidence 75 (adversarial)

**Section:** §2

The warning fires "when it is not always-on," but the doc never defines always-on, and the most common cooperative sampler — OTel's default `ParentBased(ALWAYS_ON)` — sits exactly on the line: it samples every local root yet its `remote_parent_not_sampled=ALWAYS_OFF` turns SERVER spans under an unsampled upstream traceparent into NonRecordingSpans, the precise spec §6.5 hole the own-it-all sampler closes, for the gateway/mesh-fronted apps §3 designs for. An implementer must pick: treat it as always-on (those deployments silently lose the unsampled fraction with no warning) or not (the warning fires for virtually every cooperative user, colliding with §9's quiet-by-default posture).

**Fix:** Define the predicate in §2: `ALWAYS_ON` and `ParentBased(root=ALWAYS_ON)` count as always-on (no warning, preserving quiet-by-default), and extend the cooperative-mode limitation text to name the residual explicitly — under the default ParentBased sampler, requests whose upstream propagated an unsampled traceparent are not recorded. [REVISIT] — VERIFIED (2026-07-03): HOLDS (dilemma confirmed against SDK source: default is `ParentBased(ALWAYS_ON)` with `remote_parent_not_sampled=ALWAYS_OFF`), fix HOLDS-MODIFIED: the proposed predicate still warns on unrecognized custom/vendor samplers — wrong polarity for quiet-by-default. → DECIDED (grilling 3): inverted predicate — WARN once only on recognizably lossy samplers (`ALWAYS_OFF`, `TraceIdRatioBased`, `ParentBased` with non-`ALWAYS_ON` root); everything else including unclassifiable customs is DEBUG; residual documented in §2's cooperative limitation text. Applied to §2.

### 11. BlackSheep ASGI interposition mechanism is unstated and non-trivial — confidence 75 (feasibility)

**Section:** §4

The design routes BlackSheep through the generic ASGI instrumentor, the §6 transport middleware, and the §7 activation shim — all ASGI wrappers that must enclose the app callable. But `init_apitally(app, ...)` mutates in place, and BlackSheep's `Application.__call__` is a class-level dunder: the instance the server holds cannot be wrapped the way Starlette-family apps can, and 0.x used BlackSheep's native `app.middlewares` protocol, which cannot host ASGI middleware.

**Fix (manual):** State the interposition point in §4 (or a blacksheep.md appendix): wrap the instance-bound `_handle_http` / `_handle_websocket` methods (BlackSheep's `__call__` dispatches through `self._handle_http(...)`, which honors instance attributes and is ASGI-shaped) with the shim → instrumentor → transport chain, and hook `_handle_lifespan` for the lifespan.startup trigger; note the private-API dependency. — VERIFIED (2026-07-03): HOLDS, empirically against blacksheep 2.6.3 (instance `__call__` assignment ignored; `_handle_http` assignment honored, incl. under `MountMixin`). Fix simplified: `_handle_websocket` stays untouched (spec tracks no websockets) and `_handle_lifespan` is the wrong hook three ways (non-ASGI signature, bypassed by `MountMixin`'s `super()` call, unnecessary) — the public `app.on_start` event covers both lifespan and first-request activation, and 0.x already uses it. → DECIDED (grilling 3): wrap `app._handle_http` only, activation via `app.on_start`; one §4 paragraph, no appendix. Applied to §4.

### 12. 0.x options `consumer_callback`, `proxy`, `capture_client_disconnects` have no v1 disposition — confidence 75 (product-lens)

**Section:** §5 Public API

The §5 Removed list presents itself as the inventory of dropped surface, and the kwargs catch-all row covers only capture/redaction/exclusion options — three cross-framework 0.x options fall in limbo: `consumer_callback` (the declarative per-request consumer hook; consumer attribution is a spec §6.2/§7.1 aggregation key, so this is a mainstream migration path), `proxy`, and `capture_client_disconnects`. An implementer cannot tell whether to build them; the migration-guide writer cannot tell users what replaces them.

**Fix (manual):** Add explicit §5 entries: remove `consumer_callback` (replaced by calling `set_consumer()` from auth middleware/dependencies — the §5 ContextVar makes it framework-independent; note as the migration path), and list `proxy` and `capture_client_disconnects` under Removed with one-line rationales (the OTLP HTTP exporter honors standard proxy env vars; client-disconnect capture has no OTel equivalent). — VERIFIED (2026-07-03): HOLDS-MODIFIED (scope wording only: `capture_client_disconnects` is Starlette/FastAPI-only; the others each skip one framework). All rationales confirmed against source (exporter Session `trust_env=True`; zero disconnect handling in the ASGI instrumentor; 0.x shipped `set_consumer` everywhere already). Also covers the deprecated `identify_consumer_callback` alias. → DECIDED (grilling 3): three Removed entries. Applied to §5.

### 13. `set_request_attribute`'s user-visible value is undefined by the wire contract — confidence 75 (product-lens)

**Section:** §5 Public API

spec.md enumerates every SERVER-span attribute the server consumes and records no handling for arbitrary custom attributes, so the only grounded use of `set_request_attribute` anywhere in the documents is feeding `exclude_on_response` (§3). If the ingest does not store custom attributes, users who adopt the API expecting business attributes on their request logs get silent data loss dressed as a feature.

**Fix (manual):** State in §5 what the server does with custom SERVER-span attributes and record an upstream spec task defining that behavior (stored and surfaced on the request log, or explicitly ignored beyond §3 exclusion filtering). [REJECTED]

### 14. wsgi.md omits the MIME-filter step — confidence 75 (coherence)

**Section:** wsgi.md

design.md §6 specifies a MIME-allowlist-first pipeline before body reads, but wsgi.md's explanation and snippet show only the Content-Length gate. An implementer reading wsgi.md in isolation would not know to check MIME type before capturing, violating spec §6.3's allowlist and the zero-cost-for-filtered-requests guarantee.

**Fix:** Add the MIME-filter step to wsgi.md, placed before the Content-Length gate (both header-only), matching design.md §6's pipeline order. [APPROVED] — verified inline (2026-07-03), APPLIED to wsgi.md.

## P3 — omissions

### 15. `env` default silently changes from 0.x "dev" to "prod" — confidence 75 (product-lens)

**Section:** §5 Public API

Every 0.x integration defaults `env="dev"`, while v1 defaults to `"prod"` per spec §4. An upgrader who never passed `env` gets traffic relabeled: the dashboard they watched goes quiet while data accumulates under "prod" — at exactly the moment they judge whether the upgrade worked. The doc's own pattern is to own deliberate 0.x departures in-text (§10 does this for logs); this one is unowned.

**Fix:** Add to the §5 env row (or §1): the default changes from 0.x's "dev" to "prod" (spec §4); the migration guide must call this out explicitly. [REJECTED]

## FYI observations (confidence 50, no decision required)

- **Activating on `lifespan.startup` receipt races user OTel setup in lifespan handlers** (adversarial): the ASGI trigger fires on the incoming lifespan.startup message — BEFORE the app's startup handlers run. A user calling `set_tracer_provider` inside their FastAPI lifespan context manager (complete before any request, satisfying §2's stated premise) is still mode-detected too early and their provider is warn-and-ignored. Triggering on the app's `lifespan.startup.complete` message (observed on the shim's send path) is strictly later, still precedes all requests, and shrinks the residual at no cost; a failed startup falls back to the first-request trigger. [REVISIT] — VERIFIED (2026-07-03): HOLDS end to end (Starlette runs the lifespan CM between receive and the complete-send; uvicorn gates serving on `startup.complete`); population is real (FastAPI's documented init pattern is the lifespan CM). → DECIDED (grilling 3): trigger switched to the send of `lifespan.startup.complete`; `startup.failed` doesn't activate (first-request fallback); §2 residual narrowed to post-startup setup. Applied to §2 and §7.

## Residual concerns

- **Header capture has no specified mechanism anywhere in §6** despite the section title: spec §6.1's list-valued header attributes and §6.7's header-redaction-before-set MUST are assigned to no component (the instrumentors' `http_capture_headers_*` constructor params are hook-class dependencies §4 forswears, and they are discarded on already-instrumented apps). (adversarial — arguably finding-grade) → DECIDED (grilling 4, 2026-07-03): transport middleware owns header capture at the write sites already established for body/size (ASGI scope + `http.response.start`; Flask wrapped `start_response` for both; Django glue); capture-all-then-redact per 0.x, shared `redaction.py` before set; stable-semconv dash-form keys with server-side underscore fold (upstream task 6); `ApitallySpanProcessor` `on_end` rewrite extended to apply §6.7 header patterns to user-captured header attributes in either normalization. Applied to §6 (new "Header capture" subsection).
- The §7 detach-no-flush quiesce discards up to a minute of DELTA histogram accumulation per fork; the discarded data includes the request COUNT (spec §7.1 anchor), so app code that forks frequently from request handlers undercounts requests relative to request logs. (adversarial)
- A forked child that later serves cannot mint its own `service.instance.id` on the trace path in own-it-all mode: the global TracerProvider is inherited and set-once with the parent's Resource baked in — trace and metric instance identities diverge. No demonstrated population. (adversarial)
- The OpenAPI spec in the startup event is sent verbatim from the app's own openapi() output with no scrubbing of embedded example credentials — pre-existing 0.x behavior carried forward, not called out in the doc. (security-lens)
- Default-on log capture at NOTSET from a chatty high-traffic app could push the logs signal toward spec §10's per-app rate limits (1800/min, 200/s), degrading log coverage with no user-facing signal; no baseline volume data. (product-lens)
- The middleware-attachment mechanics verified in this round (FastAPI `build_middleware_stack` patch vs Starlette `add_middleware`) are 0.64b0-specific; re-verify the §6/§7 ordering rules whenever the contrib floor moves. (feasibility)

## Deferred questions

- What does `ApitallyPlugin` do when the user's Litestar plugin list already contains Litestar's stock `OpenTelemetryPlugin`? Two OTel configs would otherwise yield nested SERVER/INTERNAL span pairs per request; §4's detection list omits Litestar. (adversarial)
- For the §2 cooperative attribute-limit warning: does the inspection also cover `OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT` set in the environment when the user's provider was constructed without explicit SpanLimits, or only the provider's effective `_span_limits`? (adversarial)
- Carried from round 1: will server-side validation/server-error derivation be live at v1 GA? design.md §5 says the cloud "derives these server-side" (present tense) while spec §1 says they "will be derived server-side from traces later" — the answer determines whether migration messaging must own a feature gap. (product-lens)

## Upstream spec tasks (cloud repo — continues the round-1 list in review-1.md)

5. **§6.1 body-size wording:** soften "Full body size in bytes, set regardless of body capture" to "independent of body capture, when the size is determinable" — a chunked WSGI request body's size is unknowable without reading it (which the SDK correctly refuses to do), and this aligns §6.1 with §7.1's existing "when the size is known" so request-log and metric sizes derive from the same value. (From finding 5, grilling 3.)

6. **§6.1 header attribute key normalization:** pin the `<name>` suffix to the stable-semconv form — lowercase header name with dashes preserved, e.g. `http.request.header.content-type` — and have the server fold underscore-form keys (`_` → `-`) into the same header when parsing. OTel Python contrib still emits the pre-stabilization underscore normalization (`content_type`; the semconv opt-in machinery never covered header keys), so user-enabled instrumentor capture arrives in that form in cooperative mode. (From the header-capture residual, grilling 4.)
