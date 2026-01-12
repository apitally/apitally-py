from __future__ import annotations

import functools
from contextlib import contextmanager, suppress
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator, TypeVar, Union, overload


try:
    from typing import ParamSpec
except ImportError:  # pragma: no cover
    from typing_extensions import ParamSpec


if TYPE_CHECKING:
    from httpx import AsyncClient as HttpxAsyncClient  # type: ignore[import-not-found]
    from httpx import Client as HttpxClient  # type: ignore[import-not-found]
    from mysql.connector.abstracts import MySQLConnectionAbstract  # type: ignore[import-not-found]
    from mysql.connector.pooling import PooledMySQLConnection  # type: ignore[import-not-found]
    from opentelemetry.trace import Span
    from opentelemetry.util.types import Attributes as SpanAttributes
    from psycopg import AsyncConnection as PsycopgAsyncConnection  # type: ignore[import-not-found]
    from psycopg import Connection as PsycopgConnection  # type: ignore[import-not-found]
    from psycopg2._psycopg import connection as Psycopg2Connection  # type: ignore[import-not-found]
    from sqlalchemy import Engine as SQLAlchemyEngine  # type: ignore[import-not-found]
    from sqlalchemy.ext.asyncio import AsyncEngine as SQLAlchemyAsyncEngine  # type: ignore[import-not-found]


P = ParamSpec("P")
R = TypeVar("R")


@overload
def instrument(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]: ...


@overload
def instrument(func: Callable[P, R]) -> Callable[P, R]: ...


def instrument(func: Callable[P, R]) -> Union[Callable[P, R], Callable[P, Awaitable[R]]]:
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover
        raise RuntimeError("`instrument()` requires the `opentelemetry-api` package")

    tracer = trace.get_tracer("apitally.otel")
    span_name = func.__name__
    span_attributes: dict[str, str | int] = {
        "code.file.path": func.__code__.co_filename,
        "code.line.number": func.__code__.co_firstlineno,
        "code.function.name": f"{func.__module__}.{func.__qualname__}",
    }

    if iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with tracer.start_as_current_span(span_name, attributes=span_attributes):
                return await func(*args, **kwargs)

        return async_wrapper
    else:

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with tracer.start_as_current_span(span_name, attributes=span_attributes):
                return func(*args, **kwargs)

        return sync_wrapper


@contextmanager
def span(name: str, attributes: SpanAttributes = None) -> Iterator[Span]:
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover
        raise RuntimeError("`span()` requires the `opentelemetry-api` package")

    tracer = trace.get_tracer("apitally.otel")
    with tracer.start_as_current_span(name, attributes=attributes) as span:
        yield span


def instrument_asyncpg(**kwargs: Any) -> None:  # pragma: no cover
    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    except ImportError:
        raise RuntimeError("`instrument_asyncpg()` requires the `opentelemetry-instrumentation-asyncpg` package")

    AsyncPGInstrumentor().instrument(**kwargs)


def instrument_botocore(**kwargs: Any) -> None:  # pragma: no cover
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    except ImportError:
        raise RuntimeError("`instrument_botocore()` requires the `opentelemetry-instrumentation-botocore` package")

    BotocoreInstrumentor().instrument(**kwargs)


def instrument_httpx(client: Union[HttpxClient, HttpxAsyncClient, None] = None, **kwargs: Any) -> None:
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:  # pragma: no cover
        raise RuntimeError("`instrument_httpx()` requires the `opentelemetry-instrumentation-httpx` package")

    if client is not None:
        HTTPXClientInstrumentor().instrument_client(client, **kwargs)
    else:
        HTTPXClientInstrumentor().instrument(**kwargs)


def instrument_mysql(
    conn: Union[MySQLConnectionAbstract, PooledMySQLConnection, None] = None, **kwargs: Any
) -> None:  # pragma: no cover
    try:
        from opentelemetry.instrumentation.mysql import MySQLInstrumentor
    except ImportError:
        raise RuntimeError("`instrument_mysql()` requires the `opentelemetry-instrumentation-mysql` package")

    if conn is not None:
        MySQLInstrumentor().instrument_connection(conn, **kwargs)
    else:
        MySQLInstrumentor().instrument(**kwargs)


def instrument_psycopg(
    conn: Union[PsycopgConnection, PsycopgAsyncConnection, None] = None, **kwargs: Any
) -> None:  # pragma: no cover
    try:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    except ImportError:
        raise RuntimeError("`instrument_psycopg()` requires the `opentelemetry-instrumentation-psycopg` package")

    if conn is not None:
        PsycopgInstrumentor().instrument_connection(conn, **kwargs)
    else:
        PsycopgInstrumentor().instrument(**kwargs)


def instrument_psycopg2(conn: Union[Psycopg2Connection, None] = None, **kwargs: Any) -> None:  # pragma: no cover
    try:
        from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
    except ImportError:
        raise RuntimeError("`instrument_psycopg2()` requires the `opentelemetry-instrumentation-psycopg2` package")

    if conn is not None:
        Psycopg2Instrumentor().instrument_connection(conn, **kwargs)
    else:
        Psycopg2Instrumentor().instrument(**kwargs)


def instrument_pymongo(**kwargs: Any) -> None:  # pragma: no cover
    try:
        from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
    except ImportError:
        raise RuntimeError("`instrument_pymongo()` requires the `opentelemetry-instrumentation-pymongo` package")

    PymongoInstrumentor().instrument(**kwargs)


def instrument_redis(**kwargs: Any) -> None:  # pragma: no cover
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
    except ImportError:
        raise RuntimeError("`instrument_redis()` requires the `opentelemetry-instrumentation-redis` package")

    RedisInstrumentor().instrument(**kwargs)


def instrument_requests(**kwargs: Any) -> None:
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
    except ImportError:  # pragma: no cover
        raise RuntimeError("`instrument_requests()` requires the `opentelemetry-instrumentation-requests` package")

    RequestsInstrumentor().instrument(**kwargs)


def instrument_sqlalchemy(
    engine: Union[SQLAlchemyEngine, SQLAlchemyAsyncEngine, None], **kwargs: Any
) -> None:  # pragma: no cover
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError:
        raise RuntimeError("`instrument_sqlalchemy()` requires the `opentelemetry-instrumentation-sqlalchemy` package")

    with suppress(ImportError):
        from sqlalchemy.ext.asyncio import AsyncEngine  # type: ignore[import-not-found]

        if isinstance(engine, AsyncEngine):
            engine = engine.sync_engine

    SQLAlchemyInstrumentor().instrument(engine=engine, **kwargs)
