import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Dict, Optional


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
