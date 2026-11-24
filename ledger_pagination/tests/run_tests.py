"""Standalone test runner (no pytest required): executes every test_* function
in the given test modules and reports the outcome.

Usage:
    python3 tests/run_tests.py                 # pagination + trace-sources
    python3 tests/run_tests.py test_sources    # selected modules
"""

import importlib
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

DEFAULT_MODULES = ("test_pagination", "test_sources", "test_reconcile", "test_manifests")


def main(argv):
    names = argv[1:] or list(DEFAULT_MODULES)
    failures = 0
    total = 0
    for name in names:
        module = importlib.import_module(name)
        tests = sorted(
            (fname, fn)
            for fname, fn in vars(module).items()
            if fname.startswith("test_") and callable(fn)
        )
        print("== %s ==" % name)
        for fname, fn in tests:
            total += 1
            try:
                fn()
            except Exception:
                failures += 1
                print("FAIL %s.%s" % (name, fname))
                traceback.print_exc()
            else:
                print("ok   %s.%s" % (name, fname))
    print("-" * 60)
    print("%d passed, %d failed" % (total - failures, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
