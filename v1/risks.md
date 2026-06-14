# Implementation risk audit

The design is broadly sound, but several risks need addressing in v1/design.md before implementing. Multiple risks collapse into the same fixes — the simplifications are bigger than the bug count suggests.

## Critical fixes (HIGH — change design.md before implementing)

### 2. Add DRF route normalization

`opentelemetry-instrumentation-django` surfaces the raw regex (`drf/widgets/(?P<pk>[^/.]+)/$`) as `http.route` for DRF endpoints. Unusable for aggregation, ugly in UI.

**Fix**: ~15 lines in `apitally/django.py` to normalize regex routes (`(?P<name>...)` → `{name}`, strip `^$`). Add to §4 / §16 as required Django-specific work.

## Design simplifications that fall out

These aren't fixes for risks — they're simpler approaches the audits surfaced.

### 11. Configure-phase must not start BatchSpanProcessor threads

Python 3.12+ deprecates `fork()` from multi-threaded processes; gunicorn `--preload` triggers this. §7 already says "no threads in configure phase" but worth a test gating this.
