from typing import TYPE_CHECKING, Any, Union


if TYPE_CHECKING:
    from httpx import AsyncClient as HttpxAsyncClient  # type: ignore[import-not-found]
    from httpx import Client as HttpxClient  # type: ignore[import-not-found]
    from mysql.connector.abstracts import MySQLConnectionAbstract  # type: ignore[import-not-found]
    from mysql.connector.pooling import PooledMySQLConnection  # type: ignore[import-not-found]
    from psycopg import AsyncConnection as PsycopgAsyncConnection  # type: ignore[import-not-found]
    from psycopg import Connection as PsycopgConnection  # type: ignore[import-not-found]
    from psycopg2._psycopg import connection as Psycopg2Connection  # type: ignore[import-not-found]
    from sqlalchemy import Engine as SQLAlchemyEngine  # type: ignore[import-not-found]
    from sqlalchemy.ext.asyncio import AsyncEngine as SQLAlchemyAsyncEngine  # type: ignore[import-not-found]


def instrument_asyncpg(**kwargs: Any) -> None:
    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor  # type: ignore[import-not-found]
    except ImportError:
        raise RuntimeError("`instrument_asyncpg()` requires the `opentelemetry-instrumentation-asyncpg` package")

    AsyncPGInstrumentor().instrument(**kwargs)


def instrument_httpx(client: Union[HttpxClient, HttpxAsyncClient, None] = None, **kwargs: Any) -> None:
    try:
        from opentelemetry.instrumentation.httpx import HttpxInstrumentor  # type: ignore[import-not-found]
    except ImportError:
        raise RuntimeError("`instrument_httpx()` requires the `opentelemetry-instrumentation-httpx` package")

    if client is not None:
        HttpxInstrumentor().instrument_client(client, **kwargs)
    else:
        HttpxInstrumentor().instrument(**kwargs)


def instrument_mysql(conn: Union[MySQLConnectionAbstract, PooledMySQLConnection, None] = None, **kwargs: Any) -> None:
    try:
        from opentelemetry.instrumentation.mysql import MySQLInstrumentor  # type: ignore[import-not-found]
    except ImportError:
        raise RuntimeError("`instrument_mysql()` requires the `opentelemetry-instrumentation-mysql` package")

    if conn is not None:
        MySQLInstrumentor().instrument_connection(conn, **kwargs)
    else:
        MySQLInstrumentor().instrument(**kwargs)


def instrument_psycopg(conn: Union[PsycopgConnection, PsycopgAsyncConnection, None] = None, **kwargs: Any) -> None:
    try:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor  # type: ignore[import-not-found]
    except ImportError:
        raise RuntimeError("`instrument_psycopg()` requires the `opentelemetry-instrumentation-psycopg` package")

    if conn is not None:
        PsycopgInstrumentor().instrument_connection(conn, **kwargs)
    else:
        PsycopgInstrumentor().instrument(**kwargs)


def instrument_psycopg2(conn: Union[Psycopg2Connection, None] = None, **kwargs: Any) -> None:
    try:
        from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor  # type: ignore[import-not-found]
    except ImportError:
        raise RuntimeError("`instrument_psycopg2()` requires the `opentelemetry-instrumentation-psycopg2` package")

    if conn is not None:
        Psycopg2Instrumentor().instrument_connection(conn, **kwargs)
    else:
        Psycopg2Instrumentor().instrument(**kwargs)


def instrument_pymongo(**kwargs: Any) -> None:
    try:
        from opentelemetry.instrumentation.pymongo import PymongoInstrumentor  # type: ignore[import-not-found]
    except ImportError:
        raise RuntimeError("`instrument_pymongo()` requires the `opentelemetry-instrumentation-pymongo` package")

    PymongoInstrumentor().instrument(**kwargs)


def instrument_redis(**kwargs: Any) -> None:
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor  # type: ignore[import-not-found]
    except ImportError:
        raise RuntimeError("`instrument_redis()` requires the `opentelemetry-instrumentation-redis` package")

    RedisInstrumentor().instrument(**kwargs)


def instrument_requests(**kwargs: Any) -> None:
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor  # type: ignore[import-not-found]
    except ImportError:
        raise RuntimeError("`instrument_requests()` requires the `opentelemetry-instrumentation-requests` package")

    RequestsInstrumentor().instrument(**kwargs)


def instrument_sqlalchemy(engine: Union[SQLAlchemyEngine, SQLAlchemyAsyncEngine, None], **kwargs: Any) -> None:
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor  # type: ignore[import-not-found]
    except ImportError:
        raise RuntimeError("`instrument_sqlalchemy()` requires the `opentelemetry-instrumentation-sqlalchemy` package")

    from sqlalchemy.ext.asyncio import AsyncEngine  # type: ignore[import-not-found]

    if isinstance(engine, AsyncEngine):
        engine = engine.sync_engine

    SQLAlchemyInstrumentor().instrument(engine=engine, **kwargs)
