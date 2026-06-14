# WSGI implementation notes

## Body capture: head-cap with `_HeadTailStream` re-emit

WSGI body-capture middleware reads up to the spec §6.3 cap (50,000 bytes) from `environ["wsgi.input"]`, then substitutes a wrapper stream that re-emits the captured head followed by the original tail. The wrapper's `read(n)` MUST return up to `n` bytes per call, draining the head buffer first and topping up from the tail within the same call.

A naive wrapper that returns the entire head buffer on the first `read()` (ignoring `n`) breaks downstream body parsing. Django's body machinery treats a return value shorter than the requested `n` as a short read at the source and stops — a 70 KB POST gets truncated to the head cap. Verified empirically against `django.core.handlers.wsgi.WSGIHandler`.

```python
class _HeadTailStream(io.RawIOBase):
    def __init__(self, head: bytes, tail):
        self._head = head
        self._head_pos = 0
        self._tail = tail

    def readable(self):
        return True

    def read(self, n=-1):
        if n is None or n < 0:
            head_rest = self._head[self._head_pos:]
            self._head_pos = len(self._head)
            return head_rest + self._tail.read()
        out = bytearray()
        head_left = len(self._head) - self._head_pos
        if head_left > 0:
            take = min(head_left, n)
            out += self._head[self._head_pos : self._head_pos + take]
            self._head_pos += take
            n -= take
        if n > 0:
            out += self._tail.read(n)
        return bytes(out)

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)
```

Install by replacing `environ["wsgi.input"]` with the wrapper before calling the downstream app:

```python
head = environ["wsgi.input"].read(MAX_BODY_SIZE)
environ["wsgi.input"] = _HeadTailStream(head, environ["wsgi.input"])
```
