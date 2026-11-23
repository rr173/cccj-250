"""Generative verification for ``trace-sources`` (no large data committed).

Two generators:

``--bulk``       Many receipt batches, then a chain of whole-consignment
                 transfers, ending in one issue.  Measures how the cost scales
                 with the number of hops.  A per-hop walk or copy of the batch
                 list would cost O(batches * hops); the compact queue re-links
                 a treap root instead.

``--random``     Random feasible documents, compared against the naive
                 explicit-list reference in ``tests/sources_ref.py`` and
                 against an independent invariant checker.

Examples:
    python3 examples/verify_sources.py --bulk
    python3 examples/verify_sources.py --random 2000
"""

import argparse
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from ledger_pagination import trace_sources  # noqa: E402
from sources_ref import RefError, ref_trace  # noqa: E402


def _doc(doc_id, kind, business_time, material, warehouse, qty, **extra):
    doc = {
        "doc_id": doc_id,
        "entry_time": business_time,
        "type": kind,
        "business_time": business_time,
        "material_id": material,
        "warehouse_id": warehouse,
        "quantity": qty,
    }
    doc.update(extra)
    return doc


def payload(versions, cutoff):
    return {
        "business_cutoff": cutoff,
        "entry_cutoff": cutoff,
        "versions": versions,
    }


def bulk_case(batches, hops):
    versions = []
    t = 0
    for i in range(batches):
        versions.append(_doc("R%06d" % i, "receipt", t, "M", "W0", 1))
        t += 1
    for hop in range(hops):
        versions.append(
            _doc(
                "T%04d" % hop,
                "transfer",
                t,
                "M",
                "W%d" % hop,
                batches,
                to_warehouse_id="W%d" % (hop + 1),
            )
        )
        t += 1
    versions.append(_doc("I1", "issue", t, "M", "W%d" % hops, batches))
    return payload(versions, t + 1), t + 1


def run_bulk():
    print("bulk consignment transfers (many receipt batches, whole batch list moved)")
    print("%8s %6s %10s %12s %10s %10s" % ("batches", "hops", "seconds", "leaf_visits", "splits", "sources"))
    baseline = None
    for hops in (0, 25, 100, 400):
        batches = 4000
        case, cutoff = bulk_case(batches, hops)
        t0 = time.perf_counter()
        res = trace_sources(case)
        elapsed = time.perf_counter() - t0
        assert res["status"] == "ok", res
        row = res["details"][0]
        assert len(row["sources"]) == batches
        assert sum(s["quantity"] for s in row["sources"]) == batches
        stats = res["stats"]
        # one traversal, at the final issue only; transfers never visit batches
        assert stats["leaf_visits"] == batches, stats
        assert stats["nodes_created"] <= batches + hops, stats
        print(
            "%8d %6d %10.3f %12d %10d %10d"
            % (batches, hops, elapsed, stats["leaf_visits"], stats["split_calls"], len(row["sources"]))
        )
        if baseline is None:
            baseline = elapsed
    print(
        "400 hops cost far less than walking %d batches per hop (%d lot visits total)."
        % (4000, 4000 * 400)
    )


# ---------------------------------------------------------------------------
# random generator + independent invariant checker
# ---------------------------------------------------------------------------


def random_case(rng):
    warehouses = ["A", "B", "C"]
    materials = ["M", "N"]
    versions = []
    last_entry = {}
    stock = {(m, w): 0 for m in materials for w in warehouses}
    open_issues = []
    t = 1
    receipt_no = 0

    def add(doc):
        last_entry[doc["doc_id"]] = max(last_entry.get(doc["doc_id"], 0), doc["entry_time"])
        versions.append(doc)

    def nonempty():
        return [key for key, qty in stock.items() if qty > 0]

    for _ in range(rng.randint(4, 8)):
        mat, wh = rng.choice(materials), rng.choice(warehouses)
        qty = rng.randint(20, 40)
        add(_doc("R%d" % receipt_no, "receipt", t, mat, wh, qty))
        receipt_no += 1
        stock[(mat, wh)] += qty
        t += 1
    for i in range(rng.randint(4, 10)):
        choice = rng.choices(["issue", "transfer", "return", "receipt"], weights=[6, 3, 3, 1])[0]
        if choice == "return" and not open_issues:
            choice = "issue"
        if choice in ("issue", "transfer") and not nonempty():
            choice = "receipt"
        t += rng.randint(0, 2)
        if choice == "receipt":
            mat, wh = rng.choice(materials), rng.choice(warehouses)
            qty = rng.randint(20, 40)
            add(_doc("R%d" % receipt_no, "receipt", t, mat, wh, qty))
            receipt_no += 1
            stock[(mat, wh)] += qty
        elif choice == "issue":
            mat, wh = rng.choice(nonempty())
            qty = rng.randint(1, min(stock[(mat, wh)], 3))
            doc_id = "I%d" % i
            add(_doc(doc_id, "issue", t, mat, wh, qty))
            stock[(mat, wh)] -= qty
            open_issues.append([doc_id, mat, wh, qty])
        elif choice == "transfer":
            mat, src = rng.choice(nonempty())
            dst = rng.choice([w for w in warehouses if w != src])
            qty = rng.randint(1, min(stock[(mat, src)], 4))
            add(_doc("T%d" % i, "transfer", t, mat, src, qty, to_warehouse_id=dst))
            stock[(mat, src)] -= qty
            stock[(mat, dst)] += qty
        else:
            doc_id, mat, wh, remaining = rng.choice(open_issues)
            if remaining <= 0:
                continue
            qty = rng.randint(1, min(remaining, 3))
            add(_doc("X%d" % i, "return", t, mat, wh, qty, issue_doc_id=doc_id))
            open_issues = [
                [d, m, w, rem - qty if d == doc_id else rem] for d, m, w, rem in open_issues
            ]
            stock[(mat, wh)] += qty
        if versions and rng.random() < 0.15:
            target = rng.choice([v for v in versions if not v.get("void")])
            t = max(t, last_entry.get(target["doc_id"], t)) + 1
            if rng.random() < 0.6:
                add({"doc_id": target["doc_id"], "entry_time": t, "void": True})
            else:
                corrected = dict(target)
                corrected["entry_time"] = t
                add(corrected)
    rng.shuffle(versions)
    return payload(versions, 1000)


def check_invariants(case, res):
    """Checks that need no reference implementation."""
    if res["status"] != "ok":
        assert "details" not in res
        return
    rows = res["details"]
    keys = [(row["business_time"], row["doc_id"]) for row in rows]
    assert keys == sorted(keys), "rows not ordered by (business_time, doc_id)"
    assert len({row["doc_id"] for row in rows}) == len(rows), "duplicate rows"
    receipts = {v["doc_id"] for v in case["versions"] if v.get("type") == "receipt"}
    for row in rows:
        assert row["type"] in ("issue", "return")
        assert sum(s["quantity"] for s in row["sources"]) == row["quantity"], row
        ids = [s["receipt_doc_id"] for s in row["sources"]]
        assert ids == sorted(ids), "sources not sorted by receipt id"
        assert len(set(ids)) == len(ids), "sources not merged per receipt"
        assert all(i in receipts for i in ids), "transfer id used as a source"
        if row["type"] == "issue":
            assert row["quantity"] > 0 and "issue_doc_id" not in row
        else:
            assert row["quantity"] < 0 and row["issue_doc_id"]


def run_random(iterations):
    compared = 0
    for seed in range(iterations):
        rng = random.Random(seed)
        case = random_case(rng)
        fast = trace_sources(case)
        check_invariants(case, fast)
        try:
            ref = ref_trace(case)
        except RefError as err:
            assert fast["status"] == "error", (seed, fast)
            assert fast["error"]["code"] == err.code, (seed, fast, err.code)
            continue
        assert fast["status"] == "ok", (seed, fast)
        assert fast["details"] == ref["details"], seed
        compared += 1
    print("%d random cases, %d cross-checked against the explicit-list reference, all equal"
          % (iterations, compared))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bulk", action="store_true", help="bulk-transfer cost generator")
    parser.add_argument("--random", type=int, metavar="N", default=0, help="random cross-check count")
    args = parser.parse_args(argv)
    if not (args.bulk or args.random):
        parser.print_help()
        return 1
    if args.bulk:
        run_bulk()
    if args.random:
        run_random(args.random)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
