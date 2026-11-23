"""Command line entry: read a ledger payload (JSON) and emit logical pages (JSON).

Usage:
    python -m ledger_pagination [input.json]   # stdin when no file is given
"""

import json
import sys

from .core import STATUS_OK, paginate


def main(argv):
    if len(argv) > 1:
        with open(argv[1], "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    else:
        payload = json.load(sys.stdin)
    result = paginate(payload)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if result.get("status") == STATUS_OK else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
