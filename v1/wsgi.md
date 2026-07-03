# WSGI implementation notes

## Request body capture: Content-Length-gated full read

The WSGI middleware captures a request body only when `CONTENT_LENGTH` parses to an int. When it exceeds the 50,000-byte cap (spec §6.3), the attribute is set to `<body too large>` without reading a byte. Otherwise the body is read in full and re-emitted as a `BytesIO`, so the downstream app sees the identical stream. Reading in full avoids partial-stream re-emission entirely.

```python
try:
    length = int(environ.get("CONTENT_LENGTH", ""))
except ValueError:
    length = None  # absent/unparseable: no capture, wsgi.input untouched

if length is not None:
    if length > MAX_BODY_SIZE:
        body_attribute = BODY_TOO_LARGE
    else:
        body = environ["wsgi.input"].read(length)
        environ["wsgi.input"] = io.BytesIO(body)
        body_attribute = body
```

The gate is a correctness requirement, not an optimization. PEP 3333 makes EOF simulation past Content-Length a SHOULD, not a MUST, and the app side "should not attempt to read more data than is specified by the `CONTENT_LENGTH` variable". wsgiref and the werkzeug dev server hand the raw socket file to `wsgi.input`: an unbounded or over-sized read on a keep-alive connection blocks until the client sends more data or times out — with body capture on, that deadlocks every request served by `flask run`. gunicorn, waitress, and uWSGI bound reads at Content-Length, but the dev server is exactly where the feature gets tried first.

Chunked and absent-length request bodies are not captured. This matches the 0.x SDK (`apitally/flask.py` gates identically), uWSGI cannot deliver chunked bodies via `wsgi.input` at all, and waitress dechunks into a synthetic `CONTENT_LENGTH` so those requests still capture.

Response bodies have no read hazard — the middleware iterates the response iterable itself and accumulates chunks under the running-length rule from design.md §6.
