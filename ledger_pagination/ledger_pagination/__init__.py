"""Logical pagination of a ledger for a page-at-a-time archiving system."""

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

__all__ = [
    "paginate",
    "brute_paginate",
    "HEADER_UNITS",
    "CONTROL_UNITS",
    "MARKER_UNITS",
    "STATUS_OK",
    "STATUS_INFEASIBLE",
    "STATUS_INVALID",
]
