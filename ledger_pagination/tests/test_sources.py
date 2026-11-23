"""Tests for the ``trace-sources`` command.

Runnable both under pytest and standalone via ``run_tests.py`` (plain asserts).

Covers, as required:
* version selection: entry cut-off visibility, latest-version-wins, voided
  documents, business cut-off (including an older version before the business
  cut-off being overridden by a late correction), duplicate identical versions,
  conflicting same-key versions, late corrections not affecting earlier entry
  cut-offs;
* replay semantics: FIFO per warehouse/material, transfers not being source
  batches, transfer order preservation, return-tail semantics (last deduction
  first, relative order restored, appended after current stock), partial returns
  and several returns per issue, per-line source sums, old lines never changing;
* errors: insufficient stock, bad/over return references, malformed input;
* randomized cross-check against an explicit-list reference;
* bulk transfers of many receipt batches: cost independent of the batch list.
"""

import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger_pagination import trace_sources
from sources_ref import RefError, ref_trace

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def receipt(doc_id, bt, material, warehouse, qty, entry=None, **extra):
    doc = {
        "doc_id": doc_id,
        "entry_time": bt if entry is None else entry,
        "type": "receipt",
        "business_time": bt,
        "material_id": material,
        "warehouse_id": warehouse,
        "quantity": qty,
    }
    doc.update(extra)
    return doc


def issue(doc_id, bt, material, warehouse, qty, entry=None):
    return {
        "doc_id": doc_id,
        "entry_time": bt if entry is None else entry,
        "type": "issue",
        "business_time": bt,
        "material_id": material,
        "warehouse_id": warehouse,
        "quantity": qty,
    }


def transfer(doc_id, bt, material, src, dst, qty, entry=None):
    return {
        "doc_id": doc_id,
        "entry_time": bt if entry is None else entry,
        "type": "transfer",
        "business_time": bt,
        "material_id": material,
        "warehouse_id": src,
        "to_warehouse_id": dst,
        "quantity": qty,
    }


def ret(doc_id, bt, material, warehouse, qty, issue_doc_id, entry=None):
    return {
        "doc_id": doc_id,
        "entry_time": bt if entry is None else entry,
        "type": "return",
        "business_time": bt,
        "material_id": material,
        "warehouse_id": warehouse,
        "quantity": qty,
        "issue_doc_id": issue_doc_id,
    }


def void(doc_id, entry):
    return {"doc_id": doc_id, "entry_time": entry, "void": True}


def payload(versions, business_cutoff=100, entry_cutoff=100):
    return {
        "business_cutoff": business_cutoff,
        "entry_cutoff": entry_cutoff,
        "versions": versions,
    }


def by_doc(result):
    assert result["status"] == "ok", result
    return {row["doc_id"]: row for row in result["details"]}


def sources(row):
    return [(s["receipt_doc_id"], s["quantity"]) for s in row["sources"]]


def expect_error(payload, code, doc_id=None):
    res = trace_sources(payload)
    assert res["status"] in ("error", "invalid"), res
    if res["status"] == "invalid":
        assert any(e.get("code") == code for e in res["errors"]), res
        assert "details" not in res
        return res
    err = res["error"]
    assert err["code"] == code, err
    if doc_id is not None:
        assert err["doc_id"] == doc_id, err
    assert "details" not in res
    return res


# ---------------------------------------------------------------------------
# version selection
# ---------------------------------------------------------------------------


def test_versions_after_entry_cutoff_are_invisible():
    versions = [
        receipt("R1", 1, "M", "A", 10),
        issue("I1", 2, "M", "A", 4),
        # recorded later than the entry cut-off: not visible yet
        issue("I2", 2, "M", "A", 4, entry=50),
    ]
    res = trace_sources(payload(versions, entry_cutoff=10))
    assert [row["doc_id"] for row in res["details"]] == ["I1"]


def test_latest_visible_version_wins_even_if_older_is_before_business_cutoff():
    versions = [
        receipt("R1", 1, "M", "A", 10),
        issue("I1", 5, "M", "A", 3),
        # later correction moves the same document past the business cut-off;
        # the older version does NOT survive even though it was before it
        {"doc_id": "I1", "entry_time": 6, "type": "issue", "business_time": 200,
         "material_id": "M", "warehouse_id": "A", "quantity": 3},
    ]
    res = trace_sources(payload(versions, business_cutoff=100, entry_cutoff=10))
    assert res["details"] == []


def test_late_correction_does_not_change_an_earlier_entry_cutoff():
    base = [
        receipt("R1", 1, "M", "A", 10),
        issue("I1", 2, "M", "A", 3),
    ]
    early = trace_sources(payload(base, entry_cutoff=3))
    late = trace_sources(
        payload(
            base
            + [
                {"doc_id": "I1", "entry_time": 4, "type": "issue", "business_time": 2,
                 "material_id": "M", "warehouse_id": "A", "quantity": 5}
            ],
            entry_cutoff=10,
        )
    )
    assert by_doc(early)["I1"]["quantity"] == 3
    assert by_doc(late)["I1"]["quantity"] == 5
    assert by_doc(early)["I1"]["entry_time"] == 2
    assert by_doc(late)["I1"]["entry_time"] == 4


def test_voided_latest_version_removes_the_document():
    versions = [
        receipt("R1", 1, "M", "A", 10),
        issue("I1", 2, "M", "A", 3),
        void("I1", 5),
    ]
    res = trace_sources(payload(versions))
    assert res["details"] == []


def test_identical_duplicates_count_once_conflicting_ones_fail():
    dup = receipt("R1", 1, "M", "A", 10)
    res = trace_sources(payload([dup, dict(dup)]))
    assert res["status"] == "ok" and res["stats"]["receipts"] == 1

    conflict = dict(dup)
    conflict["quantity"] = 11
    res = trace_sources(payload([dup, conflict]))
    assert res["status"] == "invalid"
    err = res["errors"][0]
    assert err["code"] == "version_conflict"
    assert err["doc_id"] == "R1" and err["entry_time"] == 1


def test_documents_after_business_cutoff_do_not_participate():
    versions = [
        receipt("R1", 1, "M", "A", 10),
        issue("I1", 5, "M", "A", 4),
        issue("I2", 500, "M", "A", 4),
    ]
    res = trace_sources(payload(versions, business_cutoff=100))
    assert [row["doc_id"] for row in res["details"]] == ["I1"]


# ---------------------------------------------------------------------------
# ordering and FIFO
# ---------------------------------------------------------------------------


def test_order_is_business_time_then_doc_id_codepoint():
    versions = [
        receipt("R2", 1, "M", "A", 5),
        receipt("R1", 1, "M", "A", 5),
        # business_time 2 for both; "I10" < "I2" by code point
        issue("I2", 2, "M", "A", 5),
        issue("I10", 2, "M", "A", 5),
    ]
    res = trace_sources(payload(versions))
    rows = res["details"]
    assert [row["doc_id"] for row in rows] == ["I10", "I2"]
    # R1 arrived first among the receipts and feeds I10
    assert sources(rows[0]) == [("R1", 5)]
    assert sources(rows[1]) == [("R2", 5)]


def test_fifo_per_warehouse_and_material():
    versions = [
        receipt("R1", 1, "M", "A", 3),
        receipt("R2", 2, "M", "A", 3),
        receipt("R3", 1, "N", "A", 3),
        issue("I1", 3, "M", "A", 4),
        issue("I2", 4, "N", "A", 2),
    ]
    rows = by_doc(trace_sources(payload(versions)))
    assert sources(rows["I1"]) == [("R1", 3), ("R2", 1)]
    assert sources(rows["I2"]) == [("R3", 2)]


def test_transfer_preserves_order_and_is_not_a_source():
    versions = [
        receipt("R1", 1, "M", "A", 4),
        receipt("R2", 2, "M", "A", 4),
        transfer("T1", 3, "M", "A", "B", 6),
        issue("I1", 4, "M", "B", 5),
        issue("I2", 5, "M", "A", 2),
    ]
    rows = by_doc(trace_sources(payload(versions)))
    assert sources(rows["I1"]) == [("R1", 4), ("R2", 1)]
    assert sources(rows["I2"]) == [("R2", 2)]
    assert all(s["receipt_doc_id"] != "T1" for row in rows.values() for s in row["sources"])


def test_transfer_into_existing_stock_appends_after_it():
    versions = [
        receipt("R1", 1, "M", "A", 2),
        transfer("T1", 2, "M", "A", "B", 2),
        receipt("R2", 3, "M", "B", 2),
        issue("I1", 4, "M", "B", 3),
    ]
    rows = by_doc(trace_sources(payload(versions)))
    assert sources(rows["I1"]) == [("R1", 2), ("R2", 1)]


# ---------------------------------------------------------------------------
# returns
# ---------------------------------------------------------------------------


def test_return_takes_tail_of_issue_and_restores_relative_order():
    versions = [
        receipt("R1", 1, "M", "A", 3),
        receipt("R2", 2, "M", "A", 3),
        receipt("R3", 3, "M", "A", 3),
        issue("I1", 4, "M", "A", 5),  # R1*3 + R2*2
        ret("X1", 5, "M", "A", 2, "I1"),  # tail: R2*2
        issue("I2", 6, "M", "A", 3),  # R3*3; R1*3 returned earlier waits behind R3
        ret("X2", 7, "M", "A", 2, "I1"),  # tail of what is left: R1*2
        issue("I3", 8, "M", "A", 3),  # R3? no: R1*1 + R2*2 (appended in order)
    ]
    rows = by_doc(trace_sources(payload(versions)))
    assert sources(rows["I1"]) == [("R1", 3), ("R2", 2)]
    assert sources(rows["X1"]) == [("R2", -2)]
    # the 1 unit of R2 that I1 still owns is consumed before R3
    assert sources(rows["I2"]) == [("R2", 1), ("R3", 2)]
    assert sources(rows["X2"]) == [("R1", -2)]
    assert sources(rows["I3"]) == [("R2", 2), ("R3", 1)]
    # the original issue line is never rewritten
    assert sources(rows["I1"]) == [("R1", 3), ("R2", 2)]
    assert rows["X1"]["issue_doc_id"] == "I1"


def test_returned_lots_append_at_the_end_not_at_original_position():
    versions = [
        receipt("R1", 1, "M", "A", 2),
        receipt("R2", 2, "M", "A", 2),
        issue("I1", 3, "M", "A", 2),  # R1*2
        ret("X1", 4, "M", "A", 2, "I1"),
        issue("I2", 5, "M", "A", 3),
    ]
    rows = by_doc(trace_sources(payload(versions)))
    # R2 (on the shelf) is consumed before the returned R1, even though R1
    # arrived earlier: returned goods go to the end of the shelf.
    assert sources(rows["I2"]) == [("R1", 1), ("R2", 2)]


def test_several_returns_and_partial_returns():
    versions = [
        receipt("R1", 1, "M", "A", 4),
        receipt("R2", 2, "M", "A", 4),
        issue("I1", 3, "M", "A", 6),  # R1*4 + R2*2
        ret("X1", 4, "M", "A", 2, "I1"),
        ret("X2", 5, "M", "A", 3, "I1"),
    ]
    rows = by_doc(trace_sources(payload(versions)))
    # tail pops 2 -> R2*2; then 3 -> R1*3
    assert sources(rows["X1"]) == [("R2", -2)]
    assert sources(rows["X2"]) == [("R1", -3)]
    # 1 unit of R1 still unreturned
    res = trace_sources(payload(versions + [ret("X3", 6, "M", "A", 2, "I1")]))
    assert res["error"]["code"] == "over_return"


def test_return_of_several_lots_across_a_move_keeps_order():
    versions = [
        receipt("R1", 1, "M", "A", 2),
        receipt("R2", 2, "M", "A", 3),
        receipt("R3", 3, "M", "A", 2),
        issue("I1", 4, "M", "A", 5),  # R1*2 + R2*3
        transfer("T1", 5, "M", "A", "B", 2),  # takes R3*2 out of A
        ret("X1", 6, "M", "A", 4, "I1"),  # tail: R2*3 + R1*1
    ]
    rows = by_doc(trace_sources(payload(versions)))
    # the returned lots re-enter A in original deduction order, though only
    # part of R1 is still in A: the return is taken from the issue record,
    # not from whatever happens to sit on this shelf.
    assert sources(rows["X1"]) == [("R1", -1), ("R2", -3)]


def test_return_tail_follows_warehouse_moves():
    versions = [
        receipt("R1", 1, "M", "A", 2),
        receipt("R2", 2, "M", "A", 2),
        issue("I1", 3, "M", "A", 3),  # R1*2 + R2*1
        transfer("T1", 4, "M", "A", "B", 1),
        ret("X1", 5, "M", "A", 1, "I1"),  # last deduction R2 -> appended in A
    ]
    rows = by_doc(trace_sources(payload(versions)))
    assert sources(rows["X1"]) == [("R2", -1)]
    assert sources(rows["I1"]) == [("R1", 2), ("R2", 1)]


def test_sums_match_signed_totals():
    versions = [
        receipt("R1", 1, "M", "A", 10),
        receipt("R2", 1, "M", "A", 10),
        issue("I1", 2, "M", "A", 7),
        ret("X1", 3, "M", "A", 4, "I1"),
        issue("I2", 4, "M", "A", 6),
    ]
    res = trace_sources(payload(versions))
    for row in res["details"]:
        assert sum(s["quantity"] for s in row["sources"]) == row["quantity"]
        assert [s["receipt_doc_id"] for s in row["sources"]] == sorted(
            s["receipt_doc_id"] for s in row["sources"]
        )


def test_no_participating_issue_returns_empty_details():
    versions = [
        receipt("R1", 1, "M", "A", 10),
        transfer("T1", 2, "M", "A", "B", 5),
    ]
    res = trace_sources(payload(versions))
    assert res["status"] == "ok" and res["details"] == []


# ---------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------


def test_insufficient_stock_points_at_the_document():
    versions = [
        receipt("R1", 1, "M", "A", 2),
        issue("I1", 2, "M", "A", 5),
    ]
    res = expect_error(payload(versions), "insufficient_stock", "I1")
    assert res["error"]["available"] == 2

    versions = [
        receipt("R1", 1, "M", "A", 2),
        transfer("T1", 2, "M", "A", "B", 5),
    ]
    expect_error(payload(versions), "insufficient_stock", "T1")


def test_material_without_receipt_evidence_fails():
    versions = [
        receipt("R1", 1, "M", "A", 10),
        issue("I1", 2, "M", "A", 1),
        issue("I2", 2, "N", "A", 1),  # material N received nowhere in this input
    ]
    expect_error(payload(versions), "no_receipt_evidence", "I2")


def test_invalid_return_references():
    base = [
        receipt("R1", 1, "M", "A", 10),
        receipt("R2", 1, "M", "B", 10),
        issue("I1", 2, "M", "A", 4),
        issue("I2", 3, "M", "B", 4),
    ]
    # unknown issue
    expect_error(payload(base + [ret("X1", 4, "M", "A", 1, "ZZ")]), "invalid_return_reference", "X1")
    # warehouse mismatch
    expect_error(payload(base + [ret("X1", 4, "M", "B", 1, "I1")]), "invalid_return_reference", "X1")
    # referring to a later issue
    versions = [
        receipt("R1", 1, "M", "A", 10),
        ret("X1", 2, "M", "A", 1, "I1"),
        issue("I1", 3, "M", "A", 4),
    ]
    expect_error(payload(versions), "invalid_return_reference", "X1")
    # referring to a voided issue
    versions = base + [void("I1", 5), ret("X1", 6, "M", "A", 1, "I1")]
    expect_error(payload(versions), "invalid_return_reference", "X1")
    # referring to a receipt, not an issue
    expect_error(payload(base + [ret("X1", 4, "M", "A", 1, "R1")]), "invalid_return_reference", "X1")


def test_invalid_input_fields_are_reported_with_position():
    good = receipt("R1", 1, "M", "A", 10)
    cases = [
        ({"business_cutoff": 1, "entry_cutoff": "x", "versions": []}, "invalid_field", None),
        (payload([], business_cutoff=1.5), "invalid_field", None),
        (payload([dict(good, quantity=0)]), "invalid_field", 0),
        (payload([dict(good, quantity=True)]), "invalid_field", 0),
        (payload([dict(good, material_id="")]), "invalid_field", 0),
        (payload([dict(good, entry_time=-3)]), "ok", None),
        (payload([{"entry_time": 1, "type": "receipt"}]), "invalid_field", 0),
        (payload(["nope"]), "invalid_field", 0),
        (payload([dict(good, type="adjustment")]), "invalid_field", 0),
        (
            payload([transfer("T1", 1, "M", "A", "A", 2)]),
            "same_warehouse_transfer",
            0,
        ),
    ]
    for case, code, index in cases:
        if code == "ok":
            assert trace_sources(case)["status"] == "ok", case
            continue
        res = trace_sources(case)
        assert res["status"] == "invalid", (case, res)
        assert any(e["code"] == code for e in res["errors"]), (case, res)
        if index is not None:
            assert any(e.get("input_index") == index for e in res["errors"]), res


def test_unreadable_identity_reports_input_position():
    res = trace_sources(payload([{"entry_time": 1, "type": "receipt"}]))
    assert res["status"] == "invalid"
    err = res["errors"][0]
    assert err["input_index"] == 0 and err.get("doc_id") is None


# ---------------------------------------------------------------------------
# randomized cross-check against the explicit-list reference
# ---------------------------------------------------------------------------


def random_payload(rng):
    """Build a small warehouse-document scenario; most cases stay valid.

    A shadow simulation keeps the generated documents feasible (stock levels,
    returnable remainders) so the cross-check exercises replay, not only the
    error paths.  Occasional corrections/voids deliberately perturb it.
    """
    warehouses = ["A", "B", "C"]
    materials = ["M", "N"]
    versions = []
    last_entry = {}
    stock = {(m, w): 0 for m in materials for w in warehouses}
    open_issues = []
    t = 1

    def add(doc):
        last_entry[doc["doc_id"]] = max(last_entry.get(doc["doc_id"], 0), doc["entry_time"])
        versions.append(doc)

    def nonempty_keys():
        return [key for key, qty in stock.items() if qty > 0]

    receipt_no = 0
    for _ in range(rng.randint(4, 8)):
        mat = rng.choice(materials)
        wh = rng.choice(warehouses)
        qty = rng.randint(20, 40)
        add(receipt("R%d" % receipt_no, t, mat, wh, qty))
        receipt_no += 1
        stock[(mat, wh)] += qty
        t += 1

    for i in range(rng.randint(4, 10)):
        choice = rng.choices(
            ["issue", "transfer", "return", "receipt"], weights=[6, 3, 3, 1]
        )[0]
        if choice == "return" and not open_issues:
            choice = "issue"
        if choice in ("issue", "transfer") and not nonempty_keys():
            choice = "receipt"
        t += rng.randint(0, 2)
        if choice == "receipt":
            mat = rng.choice(materials)
            wh = rng.choice(warehouses)
            qty = rng.randint(20, 40)
            add(receipt("R%d" % receipt_no, t, mat, wh, qty))
            receipt_no += 1
            stock[(mat, wh)] += qty
        elif choice == "issue":
            mat, wh = rng.choice(nonempty_keys())
            qty = rng.randint(1, min(stock[(mat, wh)], 3))
            doc_id = "I%d" % i
            add(issue(doc_id, t, mat, wh, qty))
            stock[(mat, wh)] -= qty
            open_issues.append([doc_id, mat, wh, qty])
        elif choice == "transfer":
            mat, src = rng.choice(nonempty_keys())
            dst = rng.choice([w for w in warehouses if w != src])
            qty = rng.randint(1, min(stock[(mat, src)], 4))
            add(transfer("T%d" % i, t, mat, src, dst, qty))
            stock[(mat, src)] -= qty
            stock[(mat, dst)] += qty
        else:
            doc_id, mat, wh, remaining = rng.choice(open_issues)
            if remaining <= 0:
                continue
            qty = rng.randint(1, min(remaining, 3))
            add(ret("X%d" % i, t, mat, wh, qty, doc_id))
            open_issues = [
                [d, m, w, rem - qty if d == doc_id else rem]
                for d, m, w, rem in open_issues
            ]
            stock[(mat, wh)] += qty
        # random corrections / voids, always at a fresh entry time
        if versions and rng.random() < 0.15:
            target = rng.choice([v for v in versions if not v.get("void")])
            t = max(t, last_entry.get(target["doc_id"], t)) + 1
            if rng.random() < 0.6:
                add(void(target["doc_id"], t))
            else:
                corrected = dict(target)
                corrected["entry_time"] = t
                add(corrected)
    rng.shuffle(versions)
    return payload(versions, business_cutoff=1000, entry_cutoff=1000)


def test_randomized_against_explicit_list_reference():
    compared = 0
    for seed in range(1500):
        rng = random.Random(seed)
        case = random_payload(rng)
        fast = trace_sources(case)
        try:
            ref = ref_trace(case)
        except RefError as err:
            if fast["status"] == "invalid":
                assert any(e["code"] == err.code for e in fast["errors"]), (seed, case, fast)
                continue
            assert fast["status"] == "error", (seed, case, fast)
            assert fast["error"]["code"] == err.code, (seed, case, fast, err.code)
            continue
        assert fast["status"] == "ok", (seed, case, fast)
        assert fast["details"] == ref["details"], (seed, case, fast["details"], ref["details"])
        compared += 1
    assert compared > 500


# ---------------------------------------------------------------------------
# bulk transfers of many receipt batches: no per-batch walk or copy
# ---------------------------------------------------------------------------


def _bulk_payload(batches, hops, material="M"):
    versions = []
    t = 0
    for i in range(batches):
        versions.append(receipt("R%06d" % i, t, material, "W0", 1))
        t += 1
    for hop in range(hops):
        versions.append(
            transfer("T%03d" % hop, t, material, "W%d" % hop, "W%d" % (hop + 1), batches)
        )
    versions.append(issue("I1", t + 1, material, "W%d" % hops, batches))
    return payload(versions, business_cutoff=t + 10, entry_cutoff=t + 10)


def test_bulk_transfer_does_not_walk_or_copy_the_batch_list():
    batches, hops = 20_000, 200
    case = _bulk_payload(batches, hops)
    t0 = time.perf_counter()
    res = trace_sources(case)
    elapsed = time.perf_counter() - t0
    assert res["status"] == "ok"
    row = res["details"][0]
    assert len(row["sources"]) == batches
    assert sum(s["quantity"] for s in row["sources"]) == batches
    # batches are never expanded unit by unit, each receipt lot is created once
    assert res["stats"]["nodes_created"] <= batches + hops
    # only the final issue materialises the batch list; the 200 hops must not
    # visit receipt lots (a per-hop walk would be batches * hops = 4,000,000)
    assert res["stats"]["leaf_visits"] == batches
    # splitting is confined to the issue itself
    assert res["stats"]["split_calls"] <= 3
    assert elapsed < 15, "bulk replay took %.2fs" % elapsed


def test_bulk_transfer_identical_batches_across_warehouses():
    batches, hops = 5_000, 100
    versions = []
    t = 0
    for i in range(batches):
        versions.append(receipt("R%06d" % i, t, "M", "W0", 1))
        t += 1
    # every hop moves half of what is at the source
    for hop in range(hops):
        versions.append(
            transfer("T%03d" % hop, t, "M", "W%d" % hop, "W%d" % (hop + 1), batches // 2)
        )
    versions.append(issue("I1", t + 1, "M", "W0", batches // 2))
    res = trace_sources(payload(versions, t + 10, t + 10))
    assert res["status"] == "ok"
    assert res["stats"]["leaf_visits"] == batches // 2
    # after 100 partial hops W0 still has batches/2 units
    assert sum(s["quantity"] for s in res["details"][0]["sources"]) == batches // 2


if __name__ == "__main__":
    # allow direct execution for a quick smoke run
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception:
                failed += 1
                print("FAIL", name)
                traceback.print_exc()
            else:
                print("ok  ", name)
    print("%d failed" % failed)
    raise SystemExit(1 if failed else 0)
