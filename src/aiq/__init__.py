"""Deterministic local work ledger."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version


_DISTRIBUTION_NAME = "aiq-workqueue"
_SOURCE_VERSION = "0.3.0a1"


def _resolve_version() -> str:
    try:
        return _distribution_version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _SOURCE_VERSION


__version__ = _resolve_version()
