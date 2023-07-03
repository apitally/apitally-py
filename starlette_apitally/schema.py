from typing import List, TypedDict


class RequestResponseItem(TypedDict):
    method: str
    path: str
    status_code: int
    processing_time: float


class ApitallyPayload(TypedDict):
    method: str
    path: str
    status_code: int
    processing_times: List[float]
