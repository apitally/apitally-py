import gzip
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Dict, Optional, Union


def parse_int(x: Union[str, bytes, int, None]) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(x)
    except ValueError:
        return None


def try_json_loads(s: bytes, encoding: Optional[str] = None) -> Any:
    if encoding is not None and encoding.lower() == "gzip":
        try:
            s = gzip.decompress(s)
        except Exception:
            pass
    try:
        return json.loads(s)
    except Exception:
        return None


def get_versions(*packages, app_version: Optional[str] = None) -> Dict[str, str]:
    versions = _get_common_package_versions()
    for package in packages:
        versions[package] = _get_package_version(package)
    if app_version:
        versions["app"] = app_version
    return {n: v for n, v in versions.items() if v is not None}


def _get_common_package_versions() -> Dict[str, Optional[str]]:
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "apitally": _get_package_version("apitally"),
        "uvicorn": _get_package_version("uvicorn"),
        "hypercorn": _get_package_version("hypercorn"),
        "daphne": _get_package_version("daphne"),
        "gunicorn": _get_package_version("gunicorn"),
        "uwsgi": _get_package_version("uwsgi"),
    }


def _get_package_version(name: str) -> Optional[str]:
    try:
        return version(name)
    except PackageNotFoundError:
        return None
