"""Logical pagination of a ledger for a page-at-a-time archiving system.

Also exposes :func:`trace_sources`, which rebuilds per-issue/return receipt
source details from warehouse document versions (``trace-sources`` command).
"""

from .core import (
    CONTROL_UNITS,
    HEADER_UNITS,
    MARKER_UNITS,
    STATUS_INFEASIBLE,
    STATUS_INVALID,
    STATUS_OK,
    paginate,
)
from .brute import brute_paginate
from .sources import STATUS_ERROR as SOURCES_STATUS_ERROR
from .sources import STATUS_INVALID as SOURCES_STATUS_INVALID
from .sources import STATUS_OK as SOURCES_STATUS_OK
from .sources import trace_sources

__all__ = [
    "paginate",
    "brute_paginate",
    "trace_sources",
    "HEADER_UNITS",
    "CONTROL_UNITS",
    "MARKER_UNITS",
    "STATUS_OK",
    "STATUS_INFEASIBLE",
    "STATUS_INVALID",
    "SOURCES_STATUS_OK",
    "SOURCES_STATUS_INVALID",
    "SOURCES_STATUS_ERROR",
]
