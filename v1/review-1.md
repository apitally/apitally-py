# v1 design docs — review findings

Multi-persona review of `design.md` (primary), with `spec.md` as the authoritative read-only contract and `litestar.md` / `wsgi.md` as appendices. Reviewers: coherence, feasibility, security-lens, adversarial, product-lens. Date: 2026-07-02.

Confidence anchors: 100 = evidence directly confirms; 75 = verified, will hit in practice; 50 = advisory. "(+1)" marks findings independently raised by multiple reviewers and promoted one anchor step.

**Already applied to design.md** (mechanical, spec-derived): §6 MIME allowlist now includes `text/markdown`, and the read cap reads "50,000 + 1 bytes" instead of "50 KiB + 1" (spec §6.3). No other edits were made.

**Verification round (2026-07-02):** each [APPROVED] item was re-verified by a dedicated subagent against the docs, the 0.x code, and the OTel/uWSGI/Litestar sources before applying. 12 of 14 held up and are applied ([APPLIED] below). Two did not: finding 1 (fix insufficient — sampler decision needed, see G1) and finding 16 (refuted — see G2). Everything the verification surfaced for discussion is in the "Grilling session agenda" section at the end.

## Actionable findings

### 1. Span filter drops SERVER spans with remote parents — P0, error, confidence 100 (feasibility, adversarial)

**Section:** §3 Span filtering

The root decision is keyed on `span.parent is None`, but any app behind an instrumented gateway, service mesh, or upstream service receives a `traceparent` header, making its SERVER spans non-root (remote parent). Those spans miss the in-flight map lookup and get dropped — the service exports zero request data, and zero logs via the §10 shared map. Violates spec §6 ("parent may be non-empty when an upstream propagates traceparent") and §6.5 ("A request MUST be exported even when its upstream parent was not sampled").

**Fix:** Define keep-root as: `kind == SERVER` and (`span.parent is None` or `span.parent.is_remote`) → `(True, own_span_id)`; any other local root → `(False, None)`. Also specify that an `on_start` lookup miss for a local parent defaults to `(False, None)`. [APPLIED after G1 decision — explicit `ALWAYS_ON` sampler in own-it-all mode (§2, §14) + keep-root wording in §3 and §10]

### 2. `init_apitally(app: Litestar, ...)` is unimplementable — P1, error, confidence 100 (coherence, feasibility, product-lens)

**Section:** §5 Public API / §16 File structure

§5 presents `init_apitally(app, ...)` as the universal entry point and §16 lists `litestar.py # init_apitally(app: Litestar, ...)`, but litestar.md establishes the OTel plugin must be passed at `Litestar()` construction (plugin registry is a frozenset; no late-registration API). An implementer or doc writer following the design body builds a public API that cannot work.

**Fix:** In §5, add a Litestar exception: setup is `Litestar(plugins=[ApitallyPlugin(...)])` at construction, per litestar.md; document the asymmetry in user-facing setup docs. Update §16's `litestar.py` comment to export the plugin factory. [APPLIED]

### 3. Litestar `http.route` hook mechanism is dead code — P1, error, confidence 100 (feasibility)

**Section:** litestar.md — Override `http.route` via plugin hooks

With `exclude_spans=["receive", "send"]`, the ASGI instrumentation (0.64b0, which Litestar 2.24's plugin delegates to) only invokes `client_response_hook` inside `_set_send_span`, which is skipped when send spans are excluded. The response hook never fires, `http.route` stays the raw path — violating spec §6.1's route-template MUST and breaking per-endpoint aggregation for every Litestar user.

**Fix:** Keep `server_request_hook_handler` to stash the SERVER span on the scope and keep `exclude_spans`, but set `http.route` / span name from a Litestar `before_send` lifecycle hook, which fires on `http.response.start` after `scope["path_template"]` is populated. [APPLIED — DECIDED (grilling): `before_send` approach confirmed; litestar.md rewritten, also fixing the old snippet's semconv bug (method prefix belongs in the span name, not `http.route`)]

### 4. after-fork-in-parent "do nothing" kills telemetry in serving workers that fork — P1, error, confidence 100 (+1) (feasibility, adversarial)

**Section:** §7 Fork safety

The policy assumes the forking parent is always a non-serving master. `os.register_at_fork` hooks are process-global: they fire when a serving worker uses `multiprocessing` (fork start method, the Linux default) or `os.fork()` directly. The before-fork handler shuts down that worker's exporter and heartbeat, after-in-parent never restores them, and the worker goes permanently dark while serving traffic — silently, contradicting §7.3 liveness and §9 debuggability.

**Fix (judgment call, two variants suggested):** re-arm the parent lazily — after-in-parent marks the pipeline quiesced and the next SERVER span `on_start` (or next metrics tick) rebuilds the exporter and heartbeat; or gate on "has served a request" (flag set by the transport middleware). A preload master never sees request activity, so it stays inert either way. [APPLIED — DECIDED (grilling): handlers inverted to match the serving-gated activate phase. before: quiesce if activated; after-in-parent: immediate re-activation (fresh instances, same `service.instance.id`); after-in-child: reset to configured, no auto-activation.]

### 5. "Privacy posture matches the legacy SDK" is false for logs — P1, error, confidence 100 (+1) (product-lens, adversarial, security-lens)

**Section:** §6 / §10 / §13

The parity claim justifies the defaults table, but 0.x has log capture double-opt-in (`RequestLoggingConfig.enabled = False` and `capture_logs = False` in `apitally/client/request_logging.py`), while v1 flips `capture_logs` to on-by-default and exports every request-scoped log line. Log messages routinely embed the same PII/payload data the body opt-in protects, and no log-content redaction exists anywhere in the design (§6.7 covers bodies/headers/query only). Upgrading customers unknowingly start exporting sensitive log content by default. The default may be the right product call — but the doc must own it as a deliberate posture change and address log redaction, not claim parity.

**Fix:** Scope the §6 parity claim to bodies/headers explicitly. In §10/§13, state that default-on log capture is a deliberate departure from 0.x, give the rationale, and specify whether redaction patterns apply to log record content. [APPLIED — doc now states no log-content redaction in v1; the open product question is G7]

### 6. "App kwarg is proof of server bootstrap" is falsified by pytest/Celery imports — P1, error, confidence 100 (+1) (adversarial, feasibility)

**Section:** §7 Activate phase

`init_apitally(app, ...)` typically runs at module import, and that module is imported by pytest during collection (before `PYTEST_CURRENT_TEST` exists), by Celery workers importing task modules, by alembic env.py, and by REPLs. All of these activate eagerly: network threads, startup event, heartbeat — registering phantom online instances. The doc's claim that Celery/pytest/REPLs "configure but never activate" holds only for Django.

**Fix (judgment call):** gate activation for ASGI frameworks on the first `lifespan.startup` event received by the already-attached transport middleware (servers always run lifespan; imports never do), or on first request for all frameworks (the pattern Django already uses via `request_started`). Keep `init_apitally` eager for configuration only. [APPLIED — DECIDED (grilling): combined trigger, `lifespan.startup` OR first request, whichever fires first; Django keeps `request_started`; `init_apitally` configures only. Preload masters now never activate, reshaping the §7 fork story.]

### 7. `os.register_at_fork` does not cover uWSGI's C-level forks — P1, error, confidence 75 (adversarial)

**Section:** §7 Fork safety

§7 names "uWSGI without lazy-apps" as a covered pre-fork case, but `os.register_at_fork` handlers only run for forks through Python's `os.fork()` (gunicorn's path). uWSGI forks workers in C; the hooks never fire unless the embedder calls PyOS_BeforeFork/AfterFork. Under uWSGI the before-fork shutdown never runs (inherited-lock deadlock risk remains) and after-in-child re-activation may never fire (workers silently export nothing).

**Fix:** State the mechanism's actual coverage (Python-level forks) and add a PID-change check at the activation layer: record `os.getpid()` at activation; on first telemetry activity in a process with a different PID, discard inherited thread state and re-activate. [APPLIED, then superseded in grilling (G3): the PID backstop was removed — serving-gated activation makes uWSGI masters thread-free, and C-forks of an activated process are deliberately not handled]

### 8. "Restart the exporter" is not implementable with stock OTel — P1, error, confidence 75 (feasibility)

**Section:** §7 Fork safety

OTel's `BatchProcessor.shutdown()` sets a permanent `_shutdown` flag (emit rejects telemetry forever), the OTLP exporter's shutdown is likewise terminal, and `TracerProvider` has no public API to remove or replace a registered processor. OTel's own `_at_fork_reinit` restarts the worker thread but never clears `_shutdown`. The before/after handlers as described cannot "restart" anything.

**Fix:** Specify that re-activation constructs fresh instances: the provider-registered `ApitallySpanProcessor` wrapper (§3) — and equivalent facades for the metric reader and log processor — swap their wrapped batch processor/exporter for newly constructed ones in the after-in-child handler. Note the interaction with OTel's own registered fork handlers. [APPLIED — refined: metrics swap uses `MeterProvider.remove_metric_reader`/`add_metric_reader` instead of a facade]

### 9. Cooperative mode inherits the user's sampler; coverage silently degrades — P1, omission, confidence 100 (+1) (product-lens, feasibility)

**Section:** §2 / §3

Attaching our processor additively to the user's TracerProvider means inheriting their sampler. A common `ParentBased(TraceIdRatioBased(0.1))` makes 90% of SERVER spans non-recording — our processor never sees them — so Apitally request logs cover a sample while the §4 histograms (recorded in framework glue) count everything. The mismatch surfaces as "Apitally looks broken" for exactly the Datadog/Honeycomb segment cooperative mode courts, and undercuts the spirit of spec §6.5. The design discusses samplers only in own-it-all terms.

**Fix (judgment call):** at minimum, inspect the user provider's sampler at init and warn when it is not always-on, and document the limitation; the deeper question — whether cooperative mode needs a sampling-bypass design — is a product decision. [APPLIED — DECIDED (grilling): warn at init + document as limitation; no sampling bypass, no startup-event coverage field. Metrics stay complete via glue.]

### 10. Cooperative mode: user span limits silently truncate captured bodies — P1, omission, confidence 100 (+1) (adversarial, feasibility)

**Section:** §2 Attribute length limit

The 65 KiB attribute limit is only constructed in own-it-all mode, but body capture sets `apitally.request.body` on spans from the user's TracerProvider in cooperative mode, where the user's SpanLimits (or `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`, commonly set for other backends) apply at `set_attribute` time. A 40 KB JSON body gets clipped mid-document — violating spec §6.3's "exported intact, never truncated" MUST — with no signal to anyone.

**Fix:** In cooperative mode with body/header capture enabled, read the user provider's `max_attribute_length` at init; when below 65,536, log a clear warning that captured bodies may be truncated and how to raise the limit. Document as a cooperative-mode limitation. [APPLIED — see G5 for the adjacent own-it-all hole]

### 11. Meter/LoggerProvider global-vs-private registration unstated — P1, omission, confidence 100 (+1) (product-lens, adversarial)

**Section:** §2

"Always construct our own" leaves the decisive question open: installed as OTel globals (`set_meter_provider` / `set_logger_provider`) or held privately? Global registration clobbers or collides with the existing metrics/logs pipelines of exactly the users cooperative mode is designed to win (and OTel's setters refuse to overwrite an existing global, so ordering determines who silently loses). An implementer hits this immediately.

**Fix:** State that Apitally's MeterProvider and LoggerProvider are private instances, never registered as globals, passed explicitly to instrumentors, histogram construction, and the root-logger `LoggingHandler`. [APPLIED — see G6 for a side effect to decide]

### 12. Exponential/delta histogram configuration is nowhere specified — P1, omission, confidence 75 (feasibility)

**Section:** §4 / metrics

OTel Python defaults produce explicit-bucket aggregation and cumulative temporality — both dropped by the server per spec §7.1 — and even a deliberate switch to exponential aggregation defaults to `max_scale=20`, outside spec's MUST range of [-2, +6] for low-spread durations. Built with defaults, request counts, response times, and liveness-adjacent metrics all silently vanish.

**Fix:** Specify the metrics pipeline: 60 s `PeriodicExportingMetricReader` with `OTLPMetricExporter(preferred_temporality={Histogram: DELTA}, preferred_aggregation={Histogram: ExponentialBucketHistogramAggregation(max_scale=3)})`. [APPLIED — plus explicit `export_interval_millis=60_000` to defend the heartbeat cadence against a user's `OTEL_METRIC_EXPORT_INTERVAL`]

### 13. OPTIONS and excluded-request filtering assigned to no component — P1, omission, confidence 75 (feasibility)

**Section:** §3

Spec §6.5/§6.8 are MUST NOTs (no SERVER span for OPTIONS or excluded paths/user agents, "and therefore no request-scoped logs"), yet §3's processor keeps every SERVER root, §16 has no exclusion module, and the §10 log pipeline would stamp and export health-check logs. Result: /healthz and kube-probe traffic floods request logs — a visible regression vs 0.x.

**Fix:** Extend §3: at `on_start`, a SERVER root whose method is OPTIONS, or whose path/user-agent matches exclusion patterns, enters the map as `(False, None)` — dropping the span and, via the shared map, its logs. Note that histogram recording in the glue is independent: excluded requests are still counted (spec §6.8) while OPTIONS and unmatched routes are not (spec §7.1). [APPLIED — spec citations re-verified in both directions]

### 14. Invalid-token errors risk logging the bearer credential verbatim — P1, omission, confidence 75 (security-lens)

**Section:** §9

The setup-error row says invalid token → `logger.error(...)` without specifying content. The 0.x precedent logs the credential value directly (`client_base.py:56` logs the full `client_id`). Carried into v1, the write token — an actual bearer credential (spec §3, "Sentry-DSN class") — lands in application logs, and §10's root-logger bridge then re-exports that line to Apitally and any other sink. A typo becomes a credential disclosure.

**Fix:** Add an invariant to §9: error paths never interpolate the raw token into log messages; log a masked form only (e.g. short prefix). [APPLIED]

### 15. `url.query` redaction has no owner or interception point — P1, omission, confidence 75 (security-lens)

**Section:** §6

Spec §6.7 requires query-param redaction with specific default patterns, and the stock OTel instrumentor sets `url.query` from the raw request. §6 documents redaction mechanics in detail for bodies only; §16's `redaction.py` claims "body/headers/query" but no section says where or when query rewriting happens. An implementer following §6 could ship API keys in query strings unredacted while believing redaction is handled elsewhere.

**Fix (judgment call):** add a query-redaction subsection to §6 naming the component that intercepts and rewrites `url.query` before/as the span attribute is set (e.g. via the server request hook or the span processor), mirroring the body treatment. [APPLIED — owner: `ApitallySpanProcessor` at `on_end`, forwarding a rewritten `ReadableSpan` copy; hooks and `on_start` verified unsound]

### 16. `opentelemetry-instrumentation-logging` is not the §10 bridge — P2, error, confidence 100 (feasibility, adversarial)

**Section:** §11 Dependencies

That package only ships `LoggingInstrumentor`, which injects trace-context fields into stdlib log record formatting — it does not bridge records to the LoggerProvider. The `LoggingHandler` §10 installs lives in `opentelemetry-sdk` (already required). As listed, the dependency is either a misdirection (implementer wires the wrong component, gets no log export) or dead weight against the minimal-footprint goal.

**Fix:** Remove `opentelemetry-instrumentation-logging` from required dependencies; note in §11 that the §10 bridge ships in `opentelemetry-sdk`. [NOT APPLIED — finding refuted on re-verification: the package ships the bridge handler since 0.61b0 and the SDK copy is deprecated in its favor, see G2]

### 17. StarletteInstrumentor has no `exclude_spans` parameter — P2, error, confidence 100 (feasibility)

**Section:** §4

The blanket claim "Every ASGI integration is configured with `exclude_spans=[...]`" is unimplementable for Starlette: `StarletteInstrumentor.instrument_app` (0.64b0) accepts only hooks and providers. Starlette apps would emit receive/send INTERNAL spans our exporter forwards, contradicting spec §6.6 (server drops them as a safety net; cost is bandwidth and non-compliance).

**Fix:** Add a processor-side backstop: `ApitallySpanProcessor` enters `* http send` / `* http receive` INTERNAL spans as `(False, None)` at `on_start` for all frameworks; note in §4 that Starlette relies on this filter. [APPLIED — refined: match also requires instrumentation scope prefix `opentelemetry.instrumentation.` (Starlette's spans carry scope `...starlette`, not `...asgi`), see G10]

### 19. Starlette 0.26 → 0.35 floor claim is unsupported — P2, error, confidence 75 (feasibility)

**Section:** §11 Framework version floors

The one "forced change" appears based on stale data: `opentelemetry-instrumentation-starlette` 0.64b0 declares `starlette >= 0.13`. Acting on the claim breaks users pinned to 0.26-0.34 (current floor in pyproject.toml is 0.26.1) for no upstream reason.

**Fix:** Drop the forced bump; keep the existing floor and cite the pinned contrib version's actual `_instruments` metadata when finalizing floors. [APPLIED — 0.64b0 declares `starlette >= 0.13` unbounded; "0.35" has no source in any contrib release]

## FYI observations (advisory, confidence 50)

- **F1 — §14:** No warning when `APITALLY_OTLP_ENDPOINT` resolves away from the default. A `logger.warning` naming the overridden endpoint at init would close most of the silent-redirection gap cheaply and matches §9's "always loud" invariant. (security-lens) [REJECTED]
- **F2 — §7:** The before-fork shutdown's final flush likely exports one metrics payload from the master — per spec §7.3 every metrics export is a heartbeat, so the "phantom instance" the section says it avoids appears briefly on every deploy. Suggest stopping the metric reader without a final export. (adversarial) [APPROVED] — VERIFIED (2026-07-02): HOLDS-MODIFIED. Phantom-on-deploy premise is stale (masters never activate), but §7 contradicted itself on the quiesce mechanism (line 183 `shutdown()` with final export vs line 187 `remove_metric_reader` double-shutdown). → DECIDED (grilling 2): detach-no-flush — before-fork detaches the reader via `remove_metric_reader` (no final export, no network in fork path, reader subclass no-ops `collect()` to stay log-silent); traces/logs batch processors keep their bounded shutdown flush (no detach API). §7 also now names the only real fork-from-activated scenario (app-code multiprocessing with fork start method) — no design for scenarios that don't exist.
- **F3 — §6:** Bodies are read and buffered (up to 50 KB) before the MIME filter, though Content-Type is available from headers first. Every multipart upload and binary POST pays the head-read and stream wrapping for an attribute that will never be set. Reorder: MIME filter → read → size check. (adversarial) [APPROVED] — VERIFIED (2026-07-02): HOLDS (spec-compatible on both transports; 0.x already MIME-first on responses). → DECIDED (grilling 2): MIME filter first, from headers, before any read/buffer; wrong MIME → absent attribute regardless of size; composes with the R10 Content-Length gate so capture decisions cost zero body I/O. §6 pipeline rewritten in order.
- **F4 — §1:** Alpha-only pre-release validates the breaking upgrade on self-selected early adopters (pip skips pre-releases by default); the at-risk population — 0.x upgraders — never touches an alpha. An RC stage gated on migration-path validation with real 0.x apps would close that cheaply. (product-lens) [REJECTED]
- **F5 — wsgi.md:** The install snippet reads `MAX_BODY_SIZE` without the +1 sentinel that design.md §6 requires, so an exactly-50,000-byte body cannot be distinguished from an oversized one. Change to `read(MAX_BODY_SIZE + 1)`. (feasibility) [REVISIT] — VERIFIED (2026-07-02): HOLDS as filed, then DISSOLVED by the R10 decision (grilling 2): the Content-Length gate makes the over-cap determination from the header, so no +1 probe exists anywhere in the WSGI path; ASGI's running-length accumulation never had the issue. wsgi.md rewritten.

## Residual concerns (below confidence gate, for transparency)

- **R1:** Exception messages/stacktraces ship with zero content redaction — a pre-existing 0.x gap carried forward without being noted; the OTel exception-event path may have broader reach than legacy capture. (security-lens) [REJECTED]
- **R2:** §15 should state explicitly that only the Sentry event ID crosses the boundary — no exception payload in either direction. (security-lens) [REJECTED]
- **R3:** Wire contract couples to `opentelemetry-instrumentation-system-metrics` instrument names; an upstream semconv rename would silently empty CPU/memory charts. Pin and test emitted instrument names per release. (adversarial) [REJECTED]
- **R4:** Django's `request_started` activation gate means a deployed-but-idle app never sends its startup event or heartbeat — dashboards stay empty until first traffic, which works against the one-line-setup onboarding experience. (adversarial) [REJECTED]
- **R5:** litestar.md relies on Litestar internals (`path_template` population timing, scope stashing, plugin hook signatures) with no stated compatibility contract. (adversarial) [REJECTED]
- **R6:** §8's "last call wins" is only partially true (provider-level pieces stay first-call); no warning mechanism for silently-stale settings is stated. (adversarial) [REJECTED]
- **R7:** §1 promises unpinned 0.x upgraders "a clear migration error message on import" but describes no mechanism (removed symbols raise plain ImportError unless deliberate stubs are added). (adversarial) [REJECTED]
- **R8:** Cooperative-mode detection is order-sensitive: nothing states `init_apitally` must run after the user's OTel setup; a provider installed later flips the mode silently. (feasibility) [APPROVED] — VERIFIED (2026-07-02): HOLDS-MODIFIED. Corrections: mode detection currently happens at `init_apitally()` (design.md:160), not activation, so serving-gating does NOT mitigate; and the mode never "flips" — OTel's global is set-once (`Once.do_once`), so if Apitally goes own-it-all first, the user's later `set_tracer_provider` is warn-and-ignored and their entire tracing backend is silently discarded. `ProxyTracer` re-resolves after the real provider is set, making deferred registration technically viable. Metrics/logs unaffected (private providers). Decision: defer detection+registration to activation vs loud init warning vs document-only. → DECIDED (grilling 2): defer detection + provider registration to activation, with a hard first-request guarantee — the activation trigger (lifespan, outermost activation shim, or `request_started`) always fires before the triggering request's SERVER span starts, so the first request is recorded normally via `ProxyTracer` re-resolution. Deferred inspections (sampler, span limits, env resolution) move to activation with it. Late user OTel setup (after activation) remains a documented ordering note. Applied to §2 and §7.
- **R9:** If the user's existing OTel setup already instrumented the framework, instrumentors no-op (`_is_instrumented_by_opentelemetry`) and Apitally's hooks/exclude_spans are never applied. (feasibility) [REVISIT] — VERIFIED (2026-07-02): HOLDS. Per-app guards (`app._is_instrumented_by_opentelemetry`) discard the second caller's hooks with only a generic OTel warning (Starlette: silently, no warning at all). In cooperative mode SERVER spans still reach our processor (attached to the user's provider) and the §3 backstop drops receive/send spans, so damage is confined to hook-dependent glue: consumer stashing, route fixes (BlackSheep), anything else in `server_request_hook`. Detection is cheap (public `is_instrumented_by_opentelemetry` property; per-app attr). Edge: user instrumented with an explicit non-global provider → total silence. Decision: detect+warn, and/or make Apitally hook-independent via own middleware. → DECIDED (grilling 2): full hook-independence — all glue pinned to Apitally-owned paths (transport middleware, activation, §5 ContextVar); BlackSheep route fix moved from server_request_hook to the 0.x router wrap writing through the ContextVar, so no framework degrades. Detection via the instrumentation guards, skip our call, adapt SILENTLY (DEBUG only — user directive: minimal warnings, unintrusive SDK; new logging-posture policy added to §9). Non-global-provider edge documented as a limitation. New §4 subsection "Already-instrumented frameworks".
- **R10:** The WSGI middleware's eager `wsgi.input.read()` assumes the server bounds reads at CONTENT_LENGTH; chunked/absent-length bodies could stall on keep-alive sockets. (feasibility) [REVISIT] — VERIFIED (2026-07-02): HOLDS, worse than stated. PEP 3333 EOF-simulation is only SHOULD; wsgiref and the werkzeug dev server hand the raw socket file to `wsgi.input`, so the unconditional `read(MAX_BODY_SIZE)` in wsgi.md:44 blocks on EVERY keep-alive request with a sub-cap body, including bodyless GETs — `flask run` deadlocks outright. gunicorn/waitress/uWSGI bound reads. 0.x already double-gates on Content-Length (flask.py:130-137, 287-291); the v1 snippet is a regression. Decision: gate on CONTENT_LENGTH (0.x parity) vs also honor `wsgi.input_terminated` to recover chunked capture. → DECIDED (grilling 2): Content-Length gate, 0.x parity. Over cap → sentinel without reading; else `read(content_length)` + `BytesIO` re-emit; chunked/absent-length not captured. Cascade: `_HeadTailStream` is dead code (no partial reads exist) and F5's +1 probe is moot (length known from header). wsgi.md rewritten; design §6 mechanics updated with per-transport rules.
- **R11:** How the "active SERVER span" handle is obtained for `set_consumer` / body capture / `capture_exception`, and the required ordering of the Apitally middleware relative to the instrumentor's, is unspecified beyond §16's ContextVar hint. (feasibility) [APPROVED] — VERIFIED (2026-07-02): HOLDS. Five design.md sites write to "the active SERVER span" with no mechanism; `get_current_span()` is wrong under any child span and there is no public upward walk (`ReadableSpan.parent` is a SpanContext). ContextVar set in `ApitallySpanProcessor.on_start` for keep-root SERVER spans is mechanically sound (on_start runs synchronously in the caller's context; propagates into asyncio tasks and anyio threadpools via `copy_context`). Ordering rule missing: body-capture middleware must run INSIDE the instrumentor's middleware or attributes land after span end (silently dropped) — automatic for FastAPI/Starlette, init-order-dependent for Flask, position-dependent for Django. Decision: pick the span-handle mechanism + state the per-framework ordering rule. → DECIDED (grilling 2): ContextVar set in `ApitallySpanProcessor.on_start` for keep-root SERVER spans is the single mechanism for all five write sites (new §5 subsection "Locating the active SERVER span", with the cooperative-sampler and background-task caveats). Ordering rule added to §6: transport middleware inside the instrumentor's, per-framework wiring stated; full stack = activation shim → instrumentor → transport.
- **R12:** Spec §6.3's `<masked>` sentinel (body mask callback behavior) is a wire-contract MUST not mentioned in design §6. (feasibility) [APPROVED] — VERIFIED (2026-07-02): HOLDS-MODIFIED. Broader than the sentinel: the entire body-mask-callback feature is absent from v1 — no kwargs in §5, no callback step in §6's pipeline, not listed under Removed either — while spec §6.3 mandates `<masked>` when a callback drops the body and spec §11 expects legacy masking option names. 0.x: `mask_request_body_callback`/`mask_response_body_callback`, None-return or raise → `<masked>` (request_logging.py:389-416); the 0.x dict-based signature no longer exists in v1, so keeping it means redesigning the signature. Decision: keep (redesigned signature + pipeline step + sentinel) vs drop (Removed entry + upstream spec change). → DECIDED (grilling 2): keep, span-aligned signature `(ReadableSpan, bytes) -> bytes | None`; `None` or a raise → literal `<masked>` (fail closed); runs after size gate, before pattern redaction. Names drop the `_callback` suffix for consistency with `exclude_on_*`: `mask_request_body` / `mask_response_body` (upstream task 4 softens spec §11's legacy-names note). Added to §5 kwargs table and §6 pipeline.
- **R13:** system-metrics' CPU callback may emit per-mode data points depending on config; validate against spec §7.2's timestamp-pairing expectations. (feasibility) [REVISIT] — VERIFIED (2026-07-02): REFUTED at the pinned version. In 0.64b0 the `process.cpu.utilization` callback emits exactly ONE unlabeled observation (the `["user","system"]` config value is dead code for this instrument), and the SDK stamps every async observation in a collection cycle with one `time_ns()` — CPU/memory/uptime share identical timestamps, so §7.2's ≤1 s pairing is trivially satisfied. Instrument types (gauge + non-monotonic sum) both accepted. Optional hardening only: pin the literal `config={"process.cpu.utilization": None, "process.memory.usage": None}` in §12 and add a one-datapoint guard test (floor is unbounded above). → DECIDED (grilling 2): both, reframed without the upstream-change speculation — the config literal is just writing the (already required) restriction dict honestly, and the one-datapoint/shared-timestamp assertion is ordinary contract coverage inside the metrics pipeline tests. §12 updated.
- **R14:** The 0.x feature freeze is unbounded and coupled to a GA date gated only by subjective alpha feedback; a v1 slip stagnates the shipping product for paying users. (product-lens) [REJECTED]

## Deferred questions

- **Q1:** When gunicorn --preload fires the startup event in the master pre-fork, do workers re-emit it post-fork, and does server-side dedup tolerate the master's distinct `service.instance.id`? (adversarial) → Dissolved by the serving-gated activate phase: the master never activates, so the startup event only ever fires in workers.
- **Q2:** Will server-side validation/server-error derivation ship before or shortly after v1 GA, or should migration messaging plan for a longer-lived feature gap? (product-lens)
- **Q3:** Which opentelemetry-python-contrib version floor will v1 pin? Several verified behaviors (exclude_spans support, hook invocation) are version-dependent; findings 3, 17, and 19 were verified against 0.64b0. (feasibility) → Answered: api/sdk >= 1.43.0, contrib >= 0.64b0 (§11).
- **Q4:** Under gunicorn --preload with Django, do the `request_started` activation path and the §7 fork handlers interact, or are they mutually exclusive by construction? (feasibility) → Mutually exclusive by construction under the revised §7: the master never activates (no threads at fork), and `request_started` activates each worker post-fork; the fork handlers only concern processes that fork after activating.

## Grilling session agenda

The [REVISIT] items (3, 4, 6, 9) plus everything the fix-verification round surfaced (2026-07-02). G-items reference the finding they came from.

- **G1 (from finding 1 — replaces its application):** The approved processor-side keep-root fix cannot satisfy spec §6.5 in own-it-all mode. Without an explicit sampler, the OTel SDK reads `OTEL_TRACES_SAMPLER` and defaults to `parentbased_always_on`, whose `remote_parent_not_sampled=ALWAYS_OFF` turns a SERVER span under an unsampled remote parent into a `NonRecordingSpan` — `on_start`/`on_end` never fire; and `BatchSpanProcessor.on_end` drops non-sampled spans anyway, so RECORD_ONLY wouldn't help. Needs a sampler decision, e.g. `ParentBased(root=ALWAYS_ON, remote_parent_not_sampled=<RECORD_AND_SAMPLE for kind==SERVER, DROP otherwise>)`. Passing an explicit sampler is also what makes §14's "`OTEL_TRACES_SAMPLER` ignored" claim true — today it is accidentally false. Side effect to own: RECORD_AND_SAMPLE sets sampled=1, re-enabling sampling for services downstream of this one. Once decided, apply the approved keep-root wording (`kind == SERVER` and (`parent is None` or `parent.is_remote`); lookup miss for a local parent → `(False, None)`) in BOTH §3 and §10, which each state the parent-is-None rule. → DECIDED (grilling): flat `ALWAYS_ON` sampler in own-it-all mode; sampling is never Apitally's mechanism, the processor stays the single drop point; downstream sampled=1 side effect owned in §2. Applied.
- **G2 (from finding 16 — refuted):** `opentelemetry-instrumentation-logging` ships a full bridge `LoggingHandler` since 0.61b0 (and `LoggingInstrumentor` installs it on the root logger by default), while SDK 1.43 deprecates `opentelemetry.sdk._logs.LoggingHandler` in its favor — the migration direction is the opposite of the finding. The §11 dep line stays. To settle: (a) floor the dep at >= 0.61b0 (older versions really are instrumentor-only); (b) §10 should reference the instrumentation package's handler, not the deprecated SDK copy; (c) install must be direct `addHandler(LoggingHandler(logger_provider=ours))` — `LoggingInstrumentor().instrument()` hardcodes the global LoggerProvider, conflicting with §2's private providers; (d) §10's "stamps `trace_id` and `span_id`" sentence is slightly stale: the 0.61b0+ handler attaches the active OTel context instead, and the ids derive from it (the §10 map mechanism still works). → RESOLVED (grilling): all four items applied — 0.64b0 floor (§11), §10 names the instrumentation-package handler, direct `addHandler` install with the private provider, context-attach wording fixed.
- **G3 (extends REVISIT #4):** Under default uWSGI the master's exporter and heartbeat threads survive worker C-forks — no before-fork hook runs and after-in-parent never applies — so the master registers as a phantom live instance. Options: docs recommending `--py-call-uwsgi-fork-hooks --enable-threads`, or detect the `uwsgi` module and defer activation to `uwsgidecorators.postfork`. → DECIDED (grilling): dissolved by the serving-gated activate phase — masters never activate, workers activate on first request. PID backstop removed; C-level forks of an activated process are not solved for.
- **G4 (from finding 8):** Pin a minimum `opentelemetry-sdk` in §11 — the §7 metrics swap relies on `MeterProvider.add_metric_reader`/`remove_metric_reader` (verified in 1.43.0, relatively new API). Also decide whether to keep references to swapped-out batch processors/readers: OTel's fork handlers hold `WeakMethod`s, and a GC'd instance produces an unraisable `TypeError` on a later fork (tiny per-fork leak vs stderr noise). → DECIDED (grilling): floors set to api/sdk 1.43.0 + contrib 0.64b0 in §11 (the verified set); keep strong references to swapped-out instances (§7).
- **G5 (from finding 10):** Own-it-all mode has the same truncation hole via a different env var: `OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT` overrides the constructor default for span attributes. Proposal: construct `SpanLimits(max_attribute_length=65_536, max_span_attribute_length=65_536)`. → DECIDED (grilling): applied to §2.
- **G6 (from finding 11):** Passing our private `meter_provider=` to framework instrumentors routes their built-in HTTP metrics (`http.server.duration` etc.) into Apitally's OTLP export alongside the three spec histograms. Decide: don't pass it to framework instrumentors, or filter with metric views. → DECIDED (grilling): framework instrumentors never get our meter provider; ours goes only where Apitally consumes the output (§2). Fallback: no-op in own-it-all, user's pipeline in cooperative.
- **G7 (from finding 5):** Open product decision — should the §6.7 redaction patterns run against log message content? design.md now states v1 ships without log-content redaction (verbatim per spec §8). Related obligation: the migration guide must warn 0.x upgraders that logs export by default; consider promoting that to a §18 bullet. → DECIDED (grilling): ship as currently written — no log-content redaction in v1, no further doc additions; migration messaging handled by the author, out of scope here.
- **G8 (from finding 13):** 0.x's `exclude_callback(request, response)` has no v1 equivalent and cannot be decided at `on_start` (response-based; logs already emitted would leak). Confirm the drop is intentional. → DECIDED (grilling): kept and redesigned as two OTel-native callbacks — `exclude_on_request` (span start, nothing transmitted) and `exclude_on_response` (SERVER span end, keystone drop; stray telemetry discarded as orphans at ingest — GC rule to be added to the spec upstream). Both `Callable[[ReadableSpan], bool]`, True = exclude, fail-open per §9; histograms unaffected. Exclusion terminology confirmed over "filter" (matches `excluded_urls`/`exclude_spans` in OTel Python; avoids Python's filter=keep polarity trap).
- **G9 (from finding 15):** Two sign-offs: (a) spec §6.7 reads "redaction MUST run before the attribute is set" while v1 redacts query params at export via a rewritten `ReadableSpan` copy — the wire contract is satisfied, but consider softening the spec wording upstream; (b) in cooperative mode the user's other exporters still receive the raw query string — only Apitally-bound data is protected. → DECIDED (grilling): (a) soften spec §6.7 upstream to "before the attribute is exported" for query params — upstream task in the cloud repo, spec.md copy untouched; (b) accepted as documented in §6.
- **G10 (from finding 17):** Confirm the backstop's scope-prefix match (`opentelemetry.instrumentation.`) over an explicit two-name set. Websocket `* websocket receive/send` spans are not covered by spec §6.6 (http only) — possibly a spec question. Optional complement: the Starlette glue could add `OpenTelemetryMiddleware` directly with `exclude_spans` to suppress the spans at the source; the backstop stays as the compliance net either way. → DECIDED (grilling): (a) prefix scope match kept; (b) backstop extended to `* websocket send/receive` (§3), spec §6.6 generalization is an upstream task; (c) backstop only — no bespoke Starlette middleware path.
- **G11 (from finding 19, relates to Q3):** Floor the contrib instrumentation packages at >= 0.58b0 (or simply the pinned version) so `opentelemetry-instrumentation-starlette`'s historical `starlette < 0.15` pin cannot resurface. → DECIDED (grilling): superseded by the 0.64b0 contrib floor in §11.
- **G12 (from finding 2):** Naming: §5/§16 now say `ApitallyPlugin`; litestar.md's example uses a lowercase factory `apitally_litestar_plugin(...)` — align litestar.md to the winner. Also worth a sentence: what §8 idempotency means for a second `ApitallyPlugin` construction. → DECIDED (grilling): `ApitallyPlugin` wins; litestar.md aligned, and its `on_app_init` goes through the §8 config singleton so idempotency matches the other frameworks.
- **G13 (from finding 14):** Decide whether SDK-internal (`apitally.*`) loggers are excluded from the OTLP log export entirely — closes the self-noise and credential re-export vector for in-request SDK errors. → DECIDED (grilling): `apitally.*` and `opentelemetry.*` logger records are never bridged (§10); they remain in the user's own sinks.

## Upstream spec tasks (cloud repo — spec.md copy here stays untouched)

Collected from the grilling session, 2026-07-02:

1. **§6.7 query-param redaction wording:** change "before any query-param ... attribute is set" to "before the attribute is exported" for query params — the SDK redacts `url.query` at export via a rewritten span copy because the stock instrumentor sets the attribute.
2. **§6.6 per-message spans:** generalize from the two http span names to "per-message INTERNAL spans" so the `* websocket send/receive` variants are covered.
3. **Orphan GC rule (supports `exclude_on_response`):** state that descendant spans and request-scoped logs whose SERVER span never arrives are discarded — required behavior for out-of-order batch arrival anyway; `exclude_on_response` relies on it (keystone drop).
4. **§11 legacy option names:** soften "keeps existing legacy SDK option names" — v1 renames `exclude_callback` → `exclude_on_request`/`exclude_on_response` and `mask_request_body_callback`/`mask_response_body_callback` → `mask_request_body`/`mask_response_body` (callable kwargs drop the `_callback` suffix).
