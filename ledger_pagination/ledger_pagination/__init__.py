"""Logical pagination of a ledger for a page-at-a-time archiving system.

Also exposes :func:`trace_sources`, which rebuilds per-issue/return receipt
source details from warehouse document versions (``trace-sources`` command),
and :func:`reconcile_counts`, which rolls physical counts to one closing instant
(``reconcile-counts`` command).
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
from .reconcile import STATUS_ERROR as RECONCILE_STATUS_ERROR
from .reconcile import STATUS_INVALID as RECONCILE_STATUS_INVALID
from .reconcile import STATUS_OK as RECONCILE_STATUS_OK
from .reconcile import reconcile_counts
from .sources import STATUS_ERROR as SOURCES_STATUS_ERROR
from .sources import STATUS_INVALID as SOURCES_STATUS_INVALID
from .sources import STATUS_OK as SOURCES_STATUS_OK
from .sources import trace_sources

__all__ = [
    "paginate",
    "brute_paginate",
    "trace_sources",
    "reconcile_counts",
    "HEADER_UNITS",
    "CONTROL_UNITS",
    "MARKER_UNITS",
    "STATUS_OK",
    "STATUS_INFEASIBLE",
    "STATUS_INVALID",
    "SOURCES_STATUS_OK",
    "SOURCES_STATUS_INVALID",
    "SOURCES_STATUS_ERROR",
    "RECONCILE_STATUS_OK",
    "RECONCILE_STATUS_INVALID",
    "RECONCILE_STATUS_ERROR",
]
