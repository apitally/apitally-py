from contextvars import ContextVar
from typing import TYPE_CHECKING

from opentelemetry.sdk.trace import Span


if TYPE_CHECKING:
    from apitally.shared.span_processor import ApitallySpanProcessor


server_span_var: ContextVar[Span | None] = ContextVar("apitally_server_span", default=None)
server_span_kept_var: ContextVar[bool] = ContextVar("apitally_server_span_kept", default=False)
server_span_processor_var: ContextVar["ApitallySpanProcessor | None"] = ContextVar(
    "apitally_server_span_processor", default=None
)


def get_server_span() -> Span | None:
    return server_span_var.get()


def is_server_span_kept() -> bool:
    return server_span_kept_var.get()


def get_server_span_processor() -> "ApitallySpanProcessor | None":
    return server_span_processor_var.get()
