"""Generative verification for ``reconcile-counts`` (no large data committed).

``--random N``   Random feasible cases, cross-checked against the naive
                 reference in ``tests/reconcile_ref.py`` (which enumerates hop
                 times and takes the Cartesian product per material).

``--scale``      Long windows / long routes / many counted warehouses with many
                 movements: prints the elapsed time.  A per-tick scan of the
                 windows would be impossible (windows are ~10^15 wide) and
                 enumerating hop-time combinations would be exponential.

Examples:
    python3 examples/verify_reconcile.py --random 2000
    python3 examples/verify_reconcile.py --scale
"""

import argparse
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from ledger_pagination import reconcile_counts  # noqa: E402
from reconcile_ref import ref_reconcile  # noqa: E402


def random_case(seed):
    rnd = random.Random(seed)
    close_time = rnd.randint(-3, 9)
    warehouses = ["E", "W", "N"]
    rows = []
    used = set()
    for _ in range(rnd.randint(1, 3)):
        warehouse = rnd.choice(warehouses)
        material = rnd.choice(["M", "K"])
        if (warehouse, material) in used:
            continue
        used.add((warehouse, material))
        rows.append(
            {
                "warehouse_id": warehouse,
                "material_id": material,
                "count_time": rnd.randint(-3, 10),
                "counted": rnd.randint(0, 15),
                "book": rnd.randint(0, 15),
            }
        )
    journeys = []
    for number in range(rnd.randint(1, 3)):
        length = rnd.randint(2, 4)
        route = [rnd.choice(warehouses + [None]) for _ in range(length)]
        if any(route[i] == route[i - 1] for i in range(1, length)):
            continue
        windows = []
        previous = None
        feasible = True
        for _hop in range(length - 1):
            start = rnd.randint(-3, 8)
            end = start + rnd.choice([0, 0, 1, 2, 4])
            windows.append([start, end])
            previous = start if previous is None else max(start, previous)
            if previous > end:
                feasible = False
                break
        if not feasible:
            continue
        journeys.append(
            {
                "journey_id": "J%d" % number,
                "material_id": rnd.choice(["M", "K"]),
                "quantity": rnd.randint(1, 6),
                "route": route,
                "windows": windows,
            }
        )
    return {"close_time": close_time, "counts": rows, "journeys": journeys}


def run_random(iterations):
    for seed in range(iterations):
        case = random_case(seed)
        fast = reconcile_counts(case)
        assert fast["status"] == "ok", (seed, fast)
        expected_rows, expected_totals = ref_reconcile(case)
        got_rows = {
            (row["material_id"], row["warehouse_id"]): row for row in fast["rows"]
        }
        assert set(got_rows) == set(expected_rows), (seed, got_rows, expected_rows)
        for key, expected in expected_rows.items():
            got = got_rows[key]
            for field in (
                "reconciled_min",
                "reconciled_max",
                "difference_min",
                "difference_max",
            ):
                assert got[field] == expected[field], (seed, key, field, got, expected)
        got_totals = {row["material_id"]: row for row in fast["material_totals"]}
        assert set(got_totals) == set(expected_totals), (seed, got_totals)
        for material, expected in expected_totals.items():
            got = got_totals[material]
            for field in (
                "counted_total",
                "book_total",
                "reconciled_min",
                "reconciled_max",
                "difference_min",
                "difference_max",
            ):
                assert got[field] == expected[field], (
                    seed,
                    material,
                    field,
                    got,
                    expected,
                )
    print(
        "%d random cases, all equal to the exhaustive reference" % iterations
    )


def run_scale():
    big = 10 ** 15
    hops = 2000
    route = ["W%d" % position for position in range(hops + 1)]
    route = [
        warehouse if index % 2 == 0 else "T%d" % index
        for index, warehouse in enumerate(route)
    ]
    journeys = [
        {
            "journey_id": "J",
            "material_id": "M",
            "quantity": 1,
            "route": route,
            "windows": [[position, position + big] for position in range(hops)],
        }
    ]
    counts = [
        {
            "warehouse_id": "W%d" % position,
            "material_id": "M",
            "count_time": position * 3,
            "counted": 2,
            "book": 2,
        }
        for position in range(hops + 1)
    ]
    case = {"close_time": 2 * big, "counts": counts, "journeys": journeys}
    started = time.time()
    result = reconcile_counts(case)
    elapsed = time.time() - started
    assert result["status"] == "ok", result
    print(
        "scale: 1 route with %d hops, %d counted warehouses, windows ~10^15 wide -> %.2fs"
        % (hops, len(counts), elapsed)
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--random", type=int, metavar="N", default=0, help="random cross-check count"
    )
    parser.add_argument(
        "--scale", action="store_true", help="long-window / long-route cost check"
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
