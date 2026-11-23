"""Tests for the ledger pagination solver.

Runnable both under pytest and standalone via ``run_tests.py`` (plain asserts,
no pytest-specific features are used).

Covers, as required:
* small-data exhaustive split enumeration cross-checking combined page
  capacity, continuation lines, citation sharing and the fixed tie-break;
* a long list where one page can hold many rows, verifying the solver does
  not enumerate every legal full-page interval before choosing pages
  (linear-time instrumentation counters).
"""

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger_pagination import brute_paginate, paginate
from ledger_pagination.core import (
    _cost_breakdown,
    _max_ends,
    _validate,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def scratch_cost(norm, i, j):
    return _cost_breakdown(norm, i, j)["total_units"]


def allowed_boundaries(norm):
    n = norm.n
    groups = norm.groups
    allowed = [True] * (n + 1)
    for b in range(1, n):
        if groups[b - 1] == groups[b] and groups[b] in norm.unbreakable:
            allowed[b] = False
    return allowed


def check_page_assembly(payload, pages, final_end):
    """Validate the logical pages against the input, independently of the solver."""
    norm, errors = _validate(payload)
    assert not errors
    n = norm.n
    # pages tile [0, final_end) contiguously, each non-empty
    if final_end == 0:
        assert pages == []
        return
    assert pages, "expected at least one page"
    assert pages[0]["detail_start"] == 0
    assert pages[-1]["detail_end"] == final_end
    seen = []
    for idx, page in enumerate(pages):
        assert page["index"] == idx
        s, e = page["detail_start"], page["detail_end"]
        assert 0 <= s < e <= n
        if idx:
            assert s == pages[idx - 1]["detail_end"]
        seen.extend(range(s, e))
        # units independently recomputed
        assert page["units_used"] == scratch_cost(norm, s, e)
        assert page["units_used"] <= norm.capacity
        assert page["headroom"] == norm.capacity - page["units_used"]
        # continuation markers follow the original full ledger
        assert page["continues_from_previous"] == (s > 0 and norm.groups[s - 1] == norm.groups[s])
        assert page["continues_to_next"] == (e < n and norm.groups[e - 1] == norm.groups[e])
        # canonical line order: [cont-from?] (header, details..., control)* citations... [cont-to?]
        lines = page["lines"]
        pos = 0
        if page["continues_from_previous"]:
            assert lines[0]["type"] == "continuation" and lines[0]["direction"] == "from_previous"
            pos = 1
        expect_detail = s
        control_totals = {}
        while pos < len(lines) and lines[pos]["type"] == "group_header":
            gid = lines[pos]["group"]
            pos += 1
            qty_sum = 0
            while pos < len(lines) and lines[pos]["type"] == "detail":
                line = lines[pos]
                assert line["index"] == expect_detail
                assert line["group"] == gid
                expect_detail += 1
                qty_sum += norm.qtys[line["index"]]
                pos += 1
            assert pos < len(lines) and lines[pos]["type"] == "control_total"
            assert lines[pos]["group"] == gid
            control_totals[gid] = lines[pos]["qty"]
            from ledger_pagination.core import _fmt_qty

            assert lines[pos]["qty"] == _fmt_qty(qty_sum)
            pos += 1
        assert expect_detail == e
        while pos < len(lines) and lines[pos]["type"] == "citation":
            pos += 1
        if page["continues_to_next"]:
            assert lines[-1]["type"] == "continuation" and lines[-1]["direction"] == "to_next"
            assert pos == len(lines) - 1
        else:
            assert pos == len(lines)
        # per-page group segments and control totals
        assert [seg["group"] for seg in page["groups"]] == [
            line["group"] for line in lines if line["type"] == "group_header"
        ]
        for seg in page["groups"]:
            assert seg["control_total"] == control_totals[seg["group"]]
            assert seg["detail_count"] == seg["detail_end"] - seg["detail_start"]
        # citations: exactly the union over details, unique, catalog order
        union = set()
        for r in range(s, e):
            union.update(norm.cits[r])
        assert page["citations"] == [c for c in norm.catalog_order if c in union]
        cit_lines = [line for line in lines if line["type"] == "citation"]
        assert [line["id"] for line in cit_lines] == page["citations"]
        for line in cit_lines:
            assert line["units"] == norm.catalog[line["id"]]
    assert seen == list(range(final_end))


def random_payload(rng):
    n = rng.randint(1, 9)
    n_groups = rng.randint(1, min(3, n))
    cuts = sorted(rng.sample(range(1, n), n_groups - 1)) if n_groups > 1 else []
    lengths = []
    prev = 0
    for c in cuts + [n]:
        lengths.append(c - prev)
        prev = c
    groups = []
    for gi, ln in enumerate(lengths):
        groups += ["G%d" % gi] * ln
    catalog = {"C%d" % c: rng.randint(0, 3) for c in range(rng.randint(0, 3))}
    cat_ids = list(catalog)
    details = []
    for i in range(n):
        k = rng.randint(0, len(cat_ids)) if cat_ids else 0
        details.append(
            {
                "group": groups[i],
                "qty": rng.choice([1, -2, 3, "1.5", "-0.25", "2/3", 0]),
                "units": rng.randint(1, 3),
                "citations": rng.sample(cat_ids, k),
            }
        )
    unbreakable = [g for g in dict.fromkeys(groups) if rng.random() < 0.3]
    return {
        "capacity": rng.randint(1, 24),
        "citation_catalog": catalog,
        "unbreakable_groups": unbreakable,
        "details": details,
    }


# ---------------------------------------------------------------------------
# targeted tests: basic page composition
# ---------------------------------------------------------------------------


def test_single_page_basic():
    payload = {
        "capacity": 20,
        "citation_catalog": {"X": 2},
        "details": [
            {"group": "G", "qty": "5", "units": 1, "citations": ["X"]},
            {"group": "G", "qty": "-2", "units": 2, "citations": []},
            {"group": "G", "qty": "1.5", "units": 1, "citations": ["X"]},
        ],
    }
    res = paginate(payload)
    assert res["status"] == "ok"
    assert res["page_count"] == 1
    page = res["pages"][0]
    assert page["units_used"] == (1 + 2 + 1) + 2 + 2  # details + header/control + X
    assert page["groups"] == [
        {"group": "G", "detail_start": 0, "detail_end": 3, "detail_count": 3, "control_total": "9/2"}
    ]
    assert page["citations"] == ["X"]
    assert not page["continues_from_previous"] and not page["continues_to_next"]
    check_page_assembly(payload, res["pages"], 3)


def test_continuation_markers_and_per_page_control():
    # one breakable group of 6 rows, capacity forces 4 pages
    payload = {
        "capacity": 5,
        "details": [{"group": "G", "qty": 1, "units": 1} for _ in range(6)],
    }
    res = paginate(payload)
    assert res["status"] == "ok"
    assert res["page_end_boundaries"] == [2, 3, 4, 6]
    pages = res["pages"]
    assert [p["units_used"] for p in pages] == [5, 5, 5, 5]
    assert (pages[0]["continues_from_previous"], pages[0]["continues_to_next"]) == (False, True)
    assert (pages[1]["continues_from_previous"], pages[1]["continues_to_next"]) == (True, True)
    assert (pages[2]["continues_from_previous"], pages[2]["continues_to_next"]) == (True, True)
    assert (pages[3]["continues_from_previous"], pages[3]["continues_to_next"]) == (True, False)
    # control totals are per-page partial sums of the group
    assert [p["groups"][0]["control_total"] for p in pages] == ["2", "1", "1", "2"]
    check_page_assembly(payload, pages, 6)


def test_citation_shared_across_groups_sent_once():
    details = [
        {"group": "G1", "qty": 1, "units": 1, "citations": ["X"]},
        {"group": "G2", "qty": 2, "units": 1, "citations": ["X"]},
    ]
    # shared citation counted once: 2 details + 2*(header+control) + 3 = 9
    res = paginate({"capacity": 9, "citation_catalog": {"X": 3}, "details": details})
    assert res["status"] == "ok"
    assert res["page_count"] == 1
    assert res["pages"][0]["units_used"] == 9
    assert res["pages"][0]["citations"] == ["X"]
    # at capacity 8 the same citation must be paid again on each page
    res = paginate({"capacity": 8, "citation_catalog": {"X": 3}, "details": details})
    assert res["status"] == "ok"
    assert res["page_count"] == 2
    assert res["pages"][0]["citations"] == ["X"]
    assert res["pages"][1]["citations"] == ["X"]
    assert [p["units_used"] for p in res["pages"]] == [6, 6]


def test_unbreakable_group_kept_on_one_page():
    payload = {
        "capacity": 5,
        "unbreakable_groups": ["A"],
        "details": [
            {"group": "B", "qty": 1, "units": 1},
            {"group": "A", "qty": 1, "units": 1},
            {"group": "A", "qty": 1, "units": 1},
        ],
    }
    res = paginate(payload)
    assert res["status"] == "ok"
    assert res["page_end_boundaries"] == [1, 3]
    assert res["pages"][1]["groups"][0]["group"] == "A"
    assert res["pages"][1]["groups"][0]["detail_count"] == 2
    check_page_assembly(payload, res["pages"], 3)


def test_group_header_and_control_per_intersecting_group():
    details = [
        {"group": "G1", "qty": 1, "units": 1},
        {"group": "G2", "qty": 2, "units": 1},
        {"group": "G3", "qty": 3, "units": 1},
    ]
    res = paginate({"capacity": 9, "details": details})
    assert res["status"] == "ok"
    assert res["page_count"] == 1
    page = res["pages"][0]
    assert len(page["groups"]) == 3
    assert page["units_used"] == 3 + 3 * 2  # details + header/control per group
    # one unit less: the three groups no longer fit on a single page
    res = paginate({"capacity": 8, "details": details})
    assert res["status"] == "ok"
    assert res["page_count"] == 2


def test_signed_exact_quantities_in_control_totals():
    payload = {
        "capacity": 50,
        "details": [
            {"group": "G1", "qty": "-3", "units": 1},
            {"group": "G1", "qty": "2.5", "units": 1},
            {"group": "G2", "qty": "1.5", "units": 1},
            {"group": "G2", "qty": "1/3", "units": 1},
            {"group": "G2", "qty": "1/6", "units": 1},
        ],
    }
    res = paginate(payload)
    assert res["status"] == "ok"
    page = res["pages"][0]
    totals = {seg["group"]: seg["control_total"] for seg in page["groups"]}
    assert totals == {"G1": "-1/2", "G2": "2"}


# ---------------------------------------------------------------------------
# targeted tests: tie-break, infeasible, evidence
# ---------------------------------------------------------------------------


def test_tiebreak_lexicographically_smallest_boundary_vector():
    # 5 rows, one breakable group, capacity 6: feasible 2-page splits end the
    # first page at 2 or 3; the lexicographically smallest vector wins -> [2].
    payload = {
        "capacity": 6,
        "details": [{"group": "G", "qty": 1, "units": 1} for _ in range(5)],
    }
    res = paginate(payload)
    assert res["status"] == "ok"
    assert res["page_count"] == 2
    assert res["boundary_vector"] == [2]
    assert res["page_end_boundaries"] == [2, 5]
    brute = brute_paginate(payload)
    assert brute["boundaries"] == [2, 5]


def test_infeasible_single_detail_blocking():
    payload = {
        "capacity": 6,
        "details": [{"group": "G", "qty": 1, "units": u} for u in (2, 2, 2, 9)],
    }
    res = paginate(payload)
    assert res["status"] == "infeasible"
    assert res["max_covered_boundary"] == 3
    assert res["covered_details"] == 3
    assert res["prefix"]["boundaries"] == [1, 2, 3]
    assert res["prefix"]["delivered"] is False
    # prefix pages still carry continuation markers against the full ledger
    flags = [
        (p["continues_from_previous"], p["continues_to_next"])
        for p in res["prefix"]["pages"]
    ]
    assert flags == [(False, True), (True, True), (True, True)]
    ev = res["blocking_evidence"]
    assert ev["kind"] == "single_detail_exceeds_capacity"
    assert ev["at_boundary"] == 3 and ev["detail_index"] == 3
    assert ev["page_cost_if_placed"]["total_units"] == 9 + 2 + 1  # units + hdr/ctl + cont-from
    assert ev["page_cost_if_placed"]["total_units"] > ev["capacity"]
    check_page_assembly(payload, res["prefix"]["pages"], 3)


def test_infeasible_unbreakable_group_blocking():
    payload = {
        "capacity": 7,
        "unbreakable_groups": ["A"],
        "details": [
            {"group": "B", "qty": 1, "units": 1},
            {"group": "A", "qty": 1, "units": 2},
            {"group": "A", "qty": 1, "units": 2},
            {"group": "A", "qty": 1, "units": 2},
        ],
    }
    res = paginate(payload)
    assert res["status"] == "infeasible"
    assert res["max_covered_boundary"] == 1
    assert res["prefix"]["boundaries"] == [1]
    ev = res["blocking_evidence"]
    assert ev["kind"] == "unbreakable_group_exceeds_capacity"
    assert ev["group"] == "A"
    assert ev["group_interval"] == [1, 4]
    assert ev["max_end_affordable"] == 3
    assert ev["page_cost_through_group_end"]["total_units"] == 6 + 2
    assert ev["page_cost_through_group_end"]["total_units"] > ev["capacity"]
    check_page_assembly(payload, res["prefix"]["pages"], 1)


def test_infeasible_at_start():
    payload = {"capacity": 3, "details": [{"group": "G", "qty": 1, "units": 5}]}
    res = paginate(payload)
    assert res["status"] == "infeasible"
    assert res["max_covered_boundary"] == 0
    assert res["prefix"]["boundaries"] == []
    assert res["prefix"]["pages"] == []
    assert res["blocking_evidence"]["kind"] == "single_detail_exceeds_capacity"


# ---------------------------------------------------------------------------
# invalid input must be distinguished from valid-but-infeasible
# ---------------------------------------------------------------------------


def _expect_invalid(payload, needle=None):
    res = paginate(payload)
    assert res["status"] == "invalid", res
    if needle is not None:
        assert any(needle in reason for reason in res["reasons"]), res["reasons"]


def test_invalid_inputs():
    _expect_invalid({"capacity": 10, "details": []}, "details")
    _expect_invalid({"capacity": 10, "details": [{}]}, "group")
    _expect_invalid({"capacity": 0, "details": [{"group": "G", "qty": 1, "units": 1}]}, "capacity")
    _expect_invalid({"capacity": 2.5, "details": [{"group": "G", "qty": 1, "units": 1}]}, "capacity")
    _expect_invalid({"capacity": 10, "details": [{"group": "G", "qty": 1, "units": 0}]}, "units")
    _expect_invalid({"capacity": 10, "details": [{"group": "G", "qty": 1, "units": True}]}, "units")
    _expect_invalid({"capacity": 10, "details": [{"group": "G", "qty": 1, "units": 1.5}]}, "units")
    _expect_invalid({"capacity": 10, "details": [{"group": "G", "units": 1}]}, "qty")
    _expect_invalid({"capacity": 10, "details": [{"group": "G", "qty": "abc", "units": 1}]}, "qty")
    _expect_invalid(
        {"capacity": 10, "details": [{"group": "G", "qty": 1, "units": 1, "citations": ["ZZ"]}]},
        "unknown citation",
    )
    _expect_invalid(
        {
            "capacity": 10,
            "citation_catalog": {"X": -1},
            "details": [{"group": "G", "qty": 1, "units": 1}],
        },
        "non-negative",
    )
    _expect_invalid(
        {
            "capacity": 10,
            "unbreakable_groups": ["ZZ"],
            "details": [{"group": "G", "qty": 1, "units": 1}],
        },
        "unknown group",
    )
    _expect_invalid(
        {
            "capacity": 10,
            "details": [
                {"group": "G1", "qty": 1, "units": 1},
                {"group": "G2", "qty": 1, "units": 1},
                {"group": "G1", "qty": 1, "units": 1},
            ],
        },
        "contiguous",
    )
    _expect_invalid("not a payload")
    # and a valid-but-infeasible instance is NOT reported as invalid
    res = paginate({"capacity": 1, "details": [{"group": "G", "qty": 1, "units": 1}]})
    assert res["status"] == "infeasible"


# ---------------------------------------------------------------------------
# randomized cross-check against exhaustive enumeration
# ---------------------------------------------------------------------------


def test_randomized_against_brute_force():
    seeds = range(1200)
    for seed in seeds:
        rng = random.Random(seed)
        payload = random_payload(rng)
        fast = paginate(payload)
        brute = brute_paginate(payload)
        assert fast["status"] == brute["status"], (seed, payload, fast, brute)
        norm, errors = _validate(payload)
        assert not errors
        # cross-check the two-pointer max_end table against from-scratch costs
        stats = {"window_probes": 0, "window_extensions": 0, "window_removals": 0}
        max_end = _max_ends(norm, stats)
        allowed = allowed_boundaries(norm)
        for i in range(norm.n):
            j = i
            while j + 1 <= norm.n and scratch_cost(norm, i, j + 1) <= norm.capacity:
                j += 1
            assert max_end[i] == j, (seed, i, max_end[i], j)
        if fast["status"] == "ok":
            assert fast["page_count"] == brute["page_count"], (seed, payload)
            assert fast["page_end_boundaries"] == brute["boundaries"], (seed, payload)
            check_page_assembly(payload, fast["pages"], norm.n)
        else:
            assert fast["max_covered_boundary"] == brute["max_covered_boundary"], (seed, payload)
            # the reported prefix path must be a legal pagination to that boundary
            bounds = fast["prefix"]["boundaries"]
            assert (bounds[-1] if bounds else 0) == fast["max_covered_boundary"]
            prev = 0
            for b in bounds:
                assert prev < b <= norm.n
                assert allowed[b]
                assert scratch_cost(norm, prev, b) <= norm.capacity
                prev = b
            check_page_assembly(payload, fast["prefix"]["pages"], fast["max_covered_boundary"])
            # and the boundary is really maximal: brute says nothing further is reachable
            assert brute["max_covered_boundary"] == fast["max_covered_boundary"]


# ---------------------------------------------------------------------------
# long lists: linear behaviour, no enumeration of every legal page interval
# ---------------------------------------------------------------------------


def _long_payload(n, seed=7, group_max_len=80, capacity=5000, citations=8):
    rng = random.Random(seed)
    groups = []
    unbreakable = []
    gi = 0
    while len(groups) < n:
        ln = rng.randint(1, group_max_len)
        gid = "G%d" % gi
        if ln <= 3 and rng.random() < 0.1:
            unbreakable.append(gid)
        groups += [gid] * min(ln, n - len(groups))
        gi += 1
    catalog = {"C%d" % c: rng.randint(0, 2) for c in range(citations)}
    cat_ids = list(catalog)
    details = [
        {
            "group": groups[i],
            "qty": rng.randint(-50, 100),
            "units": rng.randint(1, 2),
            "citations": rng.sample(cat_ids, rng.randint(0, 2)),
        }
        for i in range(n)
    ]
    return {
        "capacity": capacity,
        "citation_catalog": catalog,
        "unbreakable_groups": unbreakable,
        "details": details,
    }


def test_long_list_is_linear_and_does_not_enumerate_intervals():
    n = 200_000
    payload = _long_payload(n)
    t0 = time.perf_counter()
    res = paginate(payload)
    elapsed = time.perf_counter() - t0
    assert res["status"] == "ok"
    # one page holds thousands of rows here; enumerating every legal page
    # interval would be ~n * rows-per-page (hundreds of millions) of probes.
    stats = res["stats"]
    assert stats["window_extensions"] == n
    assert stats["window_probes"] <= 2 * n
    assert stats["window_removals"] <= n
    assert elapsed < 30, "solver took %.1fs for n=%d" % (elapsed, n)
    # sanity: pages tile the ledger and respect capacity
    pages = res["pages"]
    assert pages[0]["detail_start"] == 0 and pages[-1]["detail_end"] == n
    for p in pages:
        assert p["units_used"] <= payload["capacity"]
    prev_end = 0
    for p in pages:
        assert p["detail_start"] == prev_end
        prev_end = p["detail_end"]


def test_long_list_single_page_when_capacity_allows():
    n = 100_000
    payload = _long_payload(n, seed=11, capacity=10 ** 9)
    res = paginate(payload)
    assert res["status"] == "ok"
    assert res["page_count"] == 1
    assert res["pages"][0]["detail_end"] == n


def test_long_list_many_groups_is_linear():
    n = 60_000
    payload = {
        "capacity": 900,
        "details": [
            {"group": "G%d" % i, "qty": 1, "units": 1, "citations": []}
            for i in range(n)
        ],
    }
    res = paginate(payload)
    assert res["status"] == "ok"
    stats = res["stats"]
    assert stats["window_probes"] <= 2 * n
    assert stats["window_extensions"] == n
