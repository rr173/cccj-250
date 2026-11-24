"""Command line entry.

Usage:
    python -m ledger_pagination [input.json]          # paginate (default)
    python -m ledger_pagination paginate [input.json]
    python -m ledger_pagination trace-sources [input.json]
    python -m ledger_pagination reconcile-counts [input.json]

Reads JSON from the given file, or stdin when no file is given.  The default
sub-command (no sub-command word) keeps the original pagination entry point and
output format unchanged.
"""

import json
import sys

from .core import STATUS_OK, paginate
from .reconcile import STATUS_OK as RECONCILE_OK
from .reconcile import reconcile_counts
from .sources import STATUS_OK as SOURCES_OK
from .sources import trace_sources

_TRACE_COMMANDS = ("trace-sources", "trace_sources")
_RECONCILE_COMMANDS = ("reconcile-counts", "reconcile_counts")


def _read_payload(path):
    if path is not None:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return json.load(sys.stdin)


def _emit(result):
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def main(argv):
    args = argv[1:]
    if args and args[0] in _RECONCILE_COMMANDS:
        payload = _read_payload(args[1] if len(args) > 1 else None)
        result = reconcile_counts(payload)
        _emit(result)
        return 0 if result.get("status") == RECONCILE_OK else 1
    if args and args[0] in _TRACE_COMMANDS:
        payload = _read_payload(args[1] if len(args) > 1 else None)
        result = trace_sources(payload)
        _emit(result)
        return 0 if result.get("status") == SOURCES_OK else 1
    if args and args[0] == "paginate":
        args = args[1:]
    payload = _read_payload(args[0] if args else None)
    result = paginate(payload)
    _emit(result)
    return 0 if result.get("status") == STATUS_OK else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
