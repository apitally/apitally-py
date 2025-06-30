from apitally.starlette import ApitallyConsumer, RequestLoggingConfig, set_consumer
from apitally.starlette import ApitallyMiddleware as _ApitallyMiddlewareForStarlette


__all__ = ["ApitallyMiddleware", "ApitallyConsumer", "RequestLoggingConfig", "set_consumer"]


class ApitallyMiddleware(_ApitallyMiddlewareForStarlette):
    """
    Apitally middleware for FastAPI applications.

    For more information, see:
    - Setup guide: https://docs.apitally.io/frameworks/fastapi
    - Reference: https://docs.apitally.io/reference/python
    """

    pass
