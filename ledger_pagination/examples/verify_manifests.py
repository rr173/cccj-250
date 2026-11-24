"""Generative verification for ``handoff-manifests`` (no large data committed).

``--random N``   Random scan sessions, cross-checked against the naive
                 reference in ``tests/manifests_ref.py`` (explicit parent map,
                 chain walks, full-catalog scans).

``--scale``      The scenario from the specification: many items packed in one
                 turnover unit, a deep container nesting chain, and a long run
                 of consecutive whole-box re-parentings before the handoff.
                 Prints elapsed time and the link-cut counters: the cost per
                 re-parenting detail stays poly-logarithmic in the catalog
                 size — no ancestor-chain walks, no unfolding of box contents.
                 Only the final handoff enumerates the objects it lists.

Examples:
    python3 examples/verify_manifests.py --random 2000
    python3 examples/verify_manifests.py --scale
"""

import argparse
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from ledger_pagination import handoff_manifests  # noqa: E402
from manifests_ref import RefError, ref_manifests  # noqa: E402
from test_manifests import random_case  # noqa: E402


def run_random(iterations):
    compared = 0
    failed = 0
    for seed in range(iterations):
        case = random_case(random.Random(seed))
        fast = handoff_manifests(case)
        try:
            ref = ref_manifests(case)
        except RefError as failure:
            failed += 1
            assert fast["status"] == "error", (seed, fast)
            assert fast["error"]["code"] == failure.code, (seed, fast, failure.code)
            continue
        assert fast["status"] == "ok", (seed, fast)
        assert fast["manifests"] == ref["manifests"], seed
        compared += 1
    print(
        "%d random sessions: %d clean runs identical to the naive reference, "
        "%d broken sessions rejected with the same error code"
        % (iterations, compared, failed)
    )


def _scale_case(depth, items, rounds):
    objects = [{"id": "K%06d" % i, "kind": "container"} for i in range(depth)]
    objects += [
        {"id": "I%06d" % i, "kind": "item", "material_id": "M"}
        for i in range(items)
    ]
    objects.append({"id": "HOLD", "kind": "container"})
    events = []
    seq = 0

    def move(changes):
        nonlocal seq
        seq += 1
        events.append(
            {
                "seq": seq,
                "type": "move",
                "changes": [
                    {"id": oid, "parent_id": pid} for oid, pid in changes
                ],
            }
        )

    for i in range(1, depth):  # one deep nesting chain
        move([("K%06d" % i, "K%06d" % (i - 1))])
    move([("I%06d" % i, "K%06d" % (depth - 1)) for i in range(items)])
    for r in range(rounds):  # whole-box re-parentings, contents never touched
        move([("K%06d" % (depth - 1), "HOLD" if r % 2 else "K%06d" % 0)])
    move([("K%06d" % (depth - 1), "K%06d" % 0)])
    seq += 1
    events.append({"seq": seq, "type": "seal", "id": "K%06d" % 0, "seal_id": "SEAL"})
    seq += 1
    events.append({"seq": seq, "type": "handoff", "id": "K%06d" % 0})
    return {"objects": objects, "events": events}


def run_scale():
    print("deep nesting + many items per unit + consecutive whole-box moves")
    print("%10s %8s %8s %10s %12s %12s %14s" % (
        "objects", "depth", "moves", "seconds", "rotations", "per_detail", "budget_use"))
    for depth, items, rounds in (
        (2000, 20000, 5000),
        (8000, 20000, 20000),
        (32000, 20000, 80000),
    ):
        case = _scale_case(depth, items, rounds)
        started = time.perf_counter()
        res = handoff_manifests(case)
        elapsed = time.perf_counter() - started
        assert res["status"] == "ok", res.get("error")
        (manifest,) = res["manifests"]
        assert len(manifest["objects"]) == depth + items
        stats = res["stats"]
        n = len(case["objects"])
        details = stats["change_details"]
        per_detail = stats["splay_rotations"] / details
        log_budget = 6 * (math.log2(n) + 2)
        print("%10d %8d %8d %10.2f %12d %12.2f %13.1f%%" % (
            n, depth, stats["moves"], elapsed,
            stats["splay_rotations"], per_detail,
            100.0 * per_detail / log_budget))
        assert per_detail <= log_budget, stats
    print(
        "rotations per re-parenting detail stay within 6*(log2 n + 2) while\n"
        "nesting depth and box contents grow — ancestor chains are never walked\n"
        "and box contents are enumerated only by the handoff that lists them."
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--random", type=int, metavar="N", default=0, help="random cross-check count"
    )
    parser.add_argument(
        "--scale", action="store_true", help="deep-nesting / whole-box cost check"
    )
    args = parser.parse_args(argv)
    if not (args.random or args.scale):
        parser.print_help()
        return 2
    if args.random:
        run_random(args.random)
    if args.scale:
        run_scale()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
