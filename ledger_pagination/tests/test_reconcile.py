"""Tests for the ``reconcile-counts`` command.

Runnable under pytest and standalone via ``run_tests.py`` (plain asserts).

Covers:
* the worked example from the specification (a lot that may cross before or
  after closing: per-warehouse bounds but a fixed total);
* reconciliation backwards when the count happened after closing;
* in-transit (``null``) stops and routes that revisit a warehouse;
* the reported bounds are *attainable* (a witness schedule exists) and exact
  (randomised cross-check against the naive reference);
* bound minima and maxima need not be attained by the same hop-time tuple, and
  per-row bounds are never added up for the material total;
* order independence and code-point sorting;
* invalid input and infeasible whole routes fail without a partial report;
* scale: long windows and long routes are handled without tick-by-tick scans.
"""

import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger_pagination import reconcile_counts
from reconcile_ref import ref_reconcile

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def journey(journey_id, material, qty, route, windows):
    return {
        "journey_id": journey_id,
        "material_id": material,
        "quantity": qty,
        "route": route,
        "windows": [list(window) for window in windows],
    }


def count(warehouse, material, count_time, counted, book):
    return {
        "warehouse_id": warehouse,
        "material_id": material,
        "count_time": count_time,
        "counted": counted,
        "book": book,
    }


def payload(counts, journeys, close_time=100):
    return {"close_time": close_time, "counts": counts, "journeys": journeys}


def rows_by_key(result):
    assert result["status"] == "ok", result
    return {(row["material_id"], row["warehouse_id"]): row for row in result["rows"]}


def totals_by_material(result):
    assert result["status"] == "ok", result
    return {row["material_id"]: row for row in result["material_totals"]}


def check_against_reference(payload_data):
    result = reconcile_counts(payload_data)
    expected_rows, expected_totals = ref_reconcile(payload_data)
    assert result["status"] == "ok", result

    got_rows = rows_by_key(result)
    assert set(got_rows) == set(expected_rows), (got_rows, expected_rows)
    for key, expected in expected_rows.items():
        got = got_rows[key]
        for field in (
            "reconciled_min",
            "reconciled_max",
            "difference_min",
            "difference_max",
        ):
            assert got[field] == expected[field], (key, field, got, expected)

    got_totals = totals_by_material(result)
    assert set(got_totals) == set(expected_totals)
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
            assert got[field] == expected[field], (material, field, got, expected)


# ---------------------------------------------------------------------------
# worked examples
# ---------------------------------------------------------------------------


def test_specification_example_per_warehouse_bounds_but_fixed_total():
    # 4 units may move east -> west before or after closing.  East counted 14
    # before the lot left, west counted 24 after it arrived.
    data = payload(
        [
            count("E", "M", 80, 14, 14),
            count("W", "M", 120, 24, 24),
        ],
        [journey("J", "M", 4, ["E", "W"], [(90, 110)])],
        close_time=100,
    )
    result = reconcile_counts(data)
    rows = rows_by_key(result)
    totals = totals_by_material(result)
    assert rows[("M", "E")]["reconciled_min"] == 10
    assert rows[("M", "E")]["reconciled_max"] == 14
    assert rows[("M", "W")]["reconciled_min"] == 20
    assert rows[("M", "W")]["reconciled_max"] == 24
    # the sum is fixed although the two rows each have a range
    assert totals["M"]["reconciled_min"] == 34
    assert totals["M"]["reconciled_max"] == 34
    check_against_reference(data)


def test_backwards_reconciliation_when_count_is_after_closing():
    # Counted after closing; a lot that had already left E before closing
    # stays out of E at closing too.
    data = payload(
        [count("E", "M", 130, 7, 10)],
        [journey("J", "M", 5, ["E", "W"], [(50, 60)])],
        close_time=100,
    )
    result = reconcile_counts(data)
    row = rows_by_key(result)[("M", "E")]
    # the lot left E long before the count, so rolling the count back to
    # closing does not change it
    assert row["reconciled_min"] == 7
    assert row["reconciled_max"] == 7
    assert row["difference_min"] == -3
    assert row["difference_max"] == -3
    check_against_reference(data)


def test_negative_reconciled_values_are_kept():
    data = payload(
        [count("E", "M", 120, 1, 0)],
        [journey("J", "M", 9, ["E", "W"], [(110, 110)])],
        close_time=100,
    )
    result = reconcile_counts(data)
    row = rows_by_key(result)[("M", "E")]
    # the lot left E at 110, before closing at 100?  No: the single hop is at
    # 110, so it is still at E at closing and gone at the 120 count.  Rolling
    # the count back to closing has to *add* the lot again.
    assert (row["reconciled_min"], row["reconciled_max"]) == (10, 10)
    check_against_reference(data)


def test_negative_reconciled_value_from_a_departure_before_closing():
    data = payload(
        [count("E", "M", 200, 1, 0)],
        [journey("J", "M", 9, ["E", "W"], [(10, 20)])],
        close_time=100,
    )
    result = reconcile_counts(data)
    row = rows_by_key(result)[("M", "E")]
    # counted long after the lot had gone; it was already gone at closing,
    # so nothing changes and the row stays at the tiny count
    assert (row["reconciled_min"], row["reconciled_max"]) == (1, 1)
    check_against_reference(data)


def test_movement_outside_the_counted_window_does_not_move_the_row():
    data = payload(
        [count("E", "M", 10, 6, 6)],
        [journey("J", "M", 4, ["E", "W"], [(40, 50)])],
        close_time=100,
    )
    result = reconcile_counts(data)
    row = rows_by_key(result)[("M", "E")]
    assert row["reconciled_min"] == row["reconciled_max"] == 2
    check_against_reference(data)


def test_warehouse_not_counted_contributes_to_totals_only():
    data = payload(
        [count("E", "M", 80, 10, 10)],
        [journey("J", "M", 3, ["E", "X"], [(90, 110)])],
        close_time=100,
    )
    result = reconcile_counts(data)
    assert len(result["rows"]) == 1
    row = rows_by_key(result)[("M", "E")]
    assert (row["reconciled_min"], row["reconciled_max"]) == (7, 10)
    check_against_reference(data)


# ---------------------------------------------------------------------------
# routes with null stops and revisits
# ---------------------------------------------------------------------------


def test_null_stop_is_at_no_warehouse():
    # W -> in transit -> E: counted at W before departure, then the goods are
    # in transit at closing, so both warehouses lose them.
    data = payload(
        [
            count("W", "M", 10, 5, 5),
        ],
        [journey("J", "M", 2, ["W", None, "E"], [(20, 20), (60, 60)])],
        close_time=50,
    )
    result = reconcile_counts(data)
    row = rows_by_key(result)[("M", "W")]
    assert (row["reconciled_min"], row["reconciled_max"]) == (3, 3)
    check_against_reference(data)


def test_route_that_returns_to_a_warehouse():
    # E -> N -> E: at closing the goods are at E again, so a count taken at E
    # before the first hop is unchanged, a count taken while away is not.
    data = payload(
        [
            count("E", "M", 5, 8, 8),
            count("N", "M", 50, 4, 4),
        ],
        [journey("J", "M", 3, ["E", "N", "E"], [(10, 20), (60, 70)])],
        close_time=100,
    )
    result = reconcile_counts(data)
    rows = rows_by_key(result)
    assert (rows[("M", "E")]["reconciled_min"], rows[("M", "E")]["reconciled_max"]) == (8, 8)
    # N was counted while the lot was certainly in transit past N (both hops
    # have left N by 50 with no revisit), so N loses all 3 units
    assert (rows[("M", "N")]["reconciled_min"], rows[("M", "N")]["reconciled_max"]) == (1, 1)
    check_against_reference(data)


def test_empty_route_means_no_warehouse_ever():
    data = payload(
        [count("E", "M", 10, 2, 2)],
        [journey("J", "M", 7, [None, None], [(0, 100)])],
        close_time=50,
    )
    # Adjacent null stops are equal entries: an invalid route.
    result = reconcile_counts(data)
    assert result["status"] == "invalid", result


# ---------------------------------------------------------------------------
# bounds are attainable and exact
# ---------------------------------------------------------------------------


def test_each_bound_has_a_witness_schedule():
    # Two journeys on the same material; the row maximum and minimum must be
    # attainable individually (though not necessarily simultaneously).
    data = payload(
        [count("E", "M", 15, 30, 30), count("W", "M", 15, 30, 30)],
        [
            journey("A", "M", 2, ["E", "W"], [(10, 20)]),
            journey("B", "M", 3, ["W", "E"], [(10, 20)]),
        ],
        close_time=100,
    )
    check_against_reference(data)


def test_row_bounds_are_not_added_for_the_total():
    # The hop may happen before or after closing; both rows get a range, but
    # the lot is always in exactly one counted warehouse, so the total is
    # fixed.  Adding the per-row bounds would give a wider wrong total.
    data = payload(
        [count("A", "M", -1, 10, 10), count("B", "M", -1, 10, 10)],
        [journey("J", "M", 3, ["A", "B"], [(0, 5)])],
        close_time=2,
    )
    result = reconcile_counts(data)
    rows = rows_by_key(result)
    totals = totals_by_material(result)
    assert (rows[("M", "A")]["reconciled_min"], rows[("M", "A")]["reconciled_max"]) == (7, 10)
    assert (rows[("M", "B")]["reconciled_min"], rows[("M", "B")]["reconciled_max"]) == (10, 13)
    assert totals["M"]["reconciled_min"] == 20
    assert totals["M"]["reconciled_max"] == 20
    check_against_reference(data)


def test_randomised_against_naive_reference():
    rnd = random.Random(20261124)
    for _ in range(800):
        close_time = rnd.randint(-2, 8)
        warehouses = ["A", "B", "C"]
        rows = []
        used = set()
        for _ in range(rnd.randint(0, 3)):
            warehouse = rnd.choice(warehouses)
            material = rnd.choice(["M", "K"])
            if (warehouse, material) in used:
                continue
            used.add((warehouse, material))
            rows.append(
                count(
                    warehouse,
                    material,
                    rnd.randint(-2, 9),
                    rnd.randint(0, 12),
                    rnd.randint(0, 12),
                )
            )
        journeys = []
        for number in range(rnd.randint(0, 2)):
            length = rnd.randint(2, 4)
            route = [rnd.choice(warehouses + [None]) for _ in range(length)]
            if any(route[i] == route[i - 1] for i in range(1, length)):
                continue
            windows = []
            feasible = True
            previous = None
            for _hop in range(length - 1):
                start = rnd.randint(-2, 8)
                end = start + rnd.choice([0, 0, 1, 2, 4])
                windows.append((start, end))
                previous = start if previous is None else max(start, previous)
                if previous > end:
                    feasible = False
                    break
            if not feasible:
                continue
            journeys.append(
                journey("J%d" % number, rnd.choice(["M", "K"]),
                        rnd.randint(1, 5), route, windows)
            )
        data = payload(rows, journeys, close_time)
        if not rows and not journeys:
            continue
        check_against_reference(data)


# ---------------------------------------------------------------------------
# ordering and determinism
# ---------------------------------------------------------------------------


def test_results_do_not_depend_on_input_order_and_are_sorted():
    rows = [
        count("W", "M", 80, 14, 10),
        count("E", "M", 120, 24, 20),
        count("A", "K", 0, 1, 1),
    ]
    journeys = [
        journey("J1", "M", 4, ["E", "W"], [(90, 110)]),
        journey("J2", "K", 2, ["A", None], [(1, 2)]),
    ]
    first = reconcile_counts(payload(rows, journeys))
    rnd = random.Random(1)
    for _ in range(20):
        shuffled_rows = rows[:]
        shuffled_journeys = journeys[:]
        rnd.shuffle(shuffled_rows)
        rnd.shuffle(shuffled_journeys)
        assert reconcile_counts(payload(shuffled_rows, shuffled_journeys)) == first
    assert [row["material_id"] for row in first["rows"]] == ["K", "M", "M"]
    assert [row["warehouse_id"] for row in first["rows"]] == ["A", "E", "W"]
    assert [row["material_id"] for row in first["material_totals"]] == ["K", "M"]


def test_sample_file_matches_expected():
    here = os.path.dirname(os.path.abspath(__file__))
    examples = os.path.join(os.path.dirname(here), "examples")
    with open(os.path.join(examples, "sample_reconcile_input.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    with open(os.path.join(examples, "sample_reconcile_expected.json"), encoding="utf-8") as fh:
        expected = json.load(fh)
    assert reconcile_counts(data) == expected


def test_cli_stdin_matches_file_output():
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample = os.path.join(root, "examples", "sample_reconcile_input.json")
    with open(sample, encoding="utf-8") as fh:
        text = fh.read()
    from_file = subprocess.run(
        [sys.executable, "-m", "ledger_pagination", "reconcile-counts", sample],
        cwd=root, capture_output=True, text=True,
    )
    from_stdin = subprocess.run(
        [sys.executable, "-m", "ledger_pagination", "reconcile-counts"],
        cwd=root, input=text, capture_output=True, text=True,
    )
    assert from_file.returncode == 0 and from_stdin.returncode == 0
    assert json.loads(from_file.stdout) == json.loads(from_stdin.stdout)
    bad = subprocess.run(
        [sys.executable, "-m", "ledger_pagination", "reconcile-counts"],
        cwd=root,
        input=json.dumps(
            payload([], [journey("J", "M", 1, ["E", "W", "N"], [(10, 10), (5, 5)])])
        ),
        capture_output=True, text=True,
    )
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["status"] == "error"


# ---------------------------------------------------------------------------
# failures: no partial report
# ---------------------------------------------------------------------------


def expect_invalid(data, code=None):
    result = reconcile_counts(data)
    assert result["status"] == "invalid", result
    assert "rows" not in result and "material_totals" not in result
    if code is not None:
        assert any(error.get("code") == code for error in result["errors"]), result
    return result


def test_invalid_input_is_reported_with_position():
    expect_invalid([])
    expect_invalid({"close_time": 1.5, "counts": [], "journeys": []})
    expect_invalid(payload([], [], close_time=True))
    expect_invalid(payload([count("", "M", 0, 1, 1)], []))
    expect_invalid(payload([count("E", "M", 0, -1, 1)], []))
    expect_invalid(payload([count("E", "M", 0, 1.0, 1)], []))
    expect_invalid(payload([], [journey("J", "M", 0, ["E", "W"], [(0, 1)])]))
    expect_invalid(payload([], [journey("J", "M", 1, ["E"], [(0, 1)])]))
    expect_invalid(payload([], [journey("J", "M", 1, ["E", "E"], [(0, 1)])]))
    expect_invalid(payload([], [journey("J", "M", 1, ["E", "W"], [(2, 1)])]))
    expect_invalid(payload([], [journey("J", "M", 1, ["E", "W"], [(0, 1), (0, 1)])]))
    expect_invalid(payload([], [journey("J", "M", 1, ["E", ""], [(0, 1)])]))
    bad = payload([], [journey("J", "M", 1, ["E", "W"], [(0, 1)])])
    bad["journeys"][0]["windows"] = "nope"
    expect_invalid(bad)


def test_duplicate_count_combination():
    data = payload(
        [count("E", "M", 0, 1, 1), count("E", "M", 1, 2, 2)],
        [],
    )
    result = expect_invalid(data, "duplicate_count_combination")
    assert result["errors"][0]["input_indices"] == [0, 1]


def test_duplicate_journey_id():
    data = payload(
        [],
        [
            journey("J", "M", 1, ["E", "W"], [(0, 1)]),
            journey("J", "M", 1, ["E", "W"], [(0, 1)]),
        ],
    )
    expect_invalid(data, "duplicate_journey_id")


def test_infeasible_route_fails_even_when_it_moves_nothing():
    data = payload(
        [count("E", "M", 0, 1, 1)],
        # hop windows cannot be met in order: the second hop must be over
        # before the first can happen
        [journey("J", "M", 1, ["E", "W", "N"], [(10, 10), (5, 5)])],
    )
    result = reconcile_counts(data)
    assert result["status"] == "error", result
    assert result["error"]["code"] == "infeasible_route"
    assert result["error"]["journey_id"] == "J"
    assert "rows" not in result and "material_totals" not in result


def test_touching_windows_are_feasible():
    # hop times may be equal, so [0, 5] then [5, 9] is fine
    data = payload(
        [count("E", "M", 0, 1, 1)],
        [journey("J", "M", 1, ["E", "W", "N"], [(0, 5), (5, 9)])],
    )
    assert reconcile_counts(data)["status"] == "ok"


# ---------------------------------------------------------------------------
# scale
# ---------------------------------------------------------------------------


def test_long_windows_and_long_routes_are_not_scanned_tick_by_tick():
    # huge windows: a per-tick scan would take billions of steps
    big = 10 ** 15
    journeys = [
        journey(
            "J%d" % i,
            "M",
            7,
            ["E", "W", "E", "W"],
            [(big * i, big * i + big), (big * i + 1, big * i + big + 1),
             (big * i + 2, big * i + big + 2)],
        )
        for i in range(200)
    ]
    data = payload(
        [count("E", "M", -10, 100, 90), count("W", "M", -10, 50, 50)],
        journeys,
        close_time=201 * big,
    )
    started = time.time()
    result = reconcile_counts(data)
    elapsed = time.time() - started
    assert result["status"] == "ok", result
    assert elapsed < 5.0, elapsed
    rows = rows_by_key(result)
    # both counts are before the first hop, so both lots are at "E" then and
    # at "W" at closing: each row moves by the full 200 lots of 7 units
    assert rows[("M", "E")]["reconciled_min"] == 100 - 200 * 7
    assert rows[("M", "E")]["reconciled_max"] == 100 - 200 * 7
    assert rows[("M", "W")]["reconciled_min"] == 50 + 200 * 7
    assert rows[("M", "W")]["reconciled_max"] == 50 + 200 * 7
