"""Reconcile physical counts to one common closing instant (``reconcile-counts``).

Financial closing compares *book* balances taken at one closing instant with
*physical* counts that were taken warehouse by warehouse, each at its own
``count_time``.  Goods move between the two, so a counted number cannot be
compared with the book number directly: it must first be rolled forward (or
backward) to ``close_time``.

Input
-----
``close_time``
    The integer closing instant shared by every ``book`` balance.
``counts``
    One row per counted ``(warehouse_id, material_id)`` pair; the pair is
    unique.  Each row has an integer ``count_time`` and two non-negative
    integers ``counted`` and ``book`` (the same closing-instant book balance).
``journeys``
    Physical movement evidence, independent of the books.  Each journey has a
    unique ``journey_id``, a ``material_id``, a positive integer ``quantity``,
    a ``route`` of at least two entries (a warehouse id, or ``null`` for "in
    transit / outside any warehouse"; adjacent entries differ) and one
    inclusive integer window ``[earliest, latest]`` per hop.  Routes may return
    to a warehouse they already passed.

Hop times
---------
A journey's hop times are non-decreasing and integer; several hops may share an
instant.  All hops at an instant are deemed complete before the position at
that instant is decided.  Before the first hop the goods are at ``route[0]``,
after the last at ``route[-1]``.  Different journeys choose their times
independently.  A hop with only a time *range* is not guessed: the report gives
the exact reachable bounds.

Reconciliation
--------------
For one concrete set of hop times the reconciled quantity of a counted row is

    counted + quantity * (present at close_time - present at count_time)

which also works backwards when the count happened after closing (a consignment
counted here but already gone at closing is subtracted).  The reconciled value
is an interval because every hop time may slide inside its window as long as
the whole route stays feasible.  The *difference* is ``reconciled - book``;
negative reconciled values are kept as they are and no feasible timing is
discarded because the counts disagree with the movements.

Report
------
``rows``            one entry per counted pair, sorted by material id then
                    warehouse id (Unicode code point order), with
                    ``reconciled_min/max`` and ``difference_min/max``;
``material_totals`` one entry per material that has counted rows, sorted by
                    material id, summing the counted warehouses over ONE set of
                    hop times per journey (per-row bounds are never added up).

Algorithm
---------
For a journey with hop windows ``[a_i, b_i]`` let ``E[i]`` be the earliest
possible time of hop ``i`` (prefix maxima of ``a``) and ``B[i]`` the latest
possible time of hop ``i`` (suffix minima of ``b``).  The route is feasible iff
``E[i] <= B[i]`` for every hop.  At time ``t`` the set of possible numbers of
completed hops is exactly the interval

    L(t) = #{ i : B[i] <= t },   P(t) = #{ i : E[i] <= t }.

Any non-decreasing selection ``l(t)`` with ``L(t) <= l(t) <= P(t)`` is realised
by some feasible timing (take hop ``i`` at ``min{t : l(t) >= i}``), hence the
extremes of an additive time-dependent weight are found by a single dynamic
program over the (at most two or "one per count") query instants:

    dp_i[position] = weight_i(position) + best(dp_{i-1}[pos' <= position]).

Times are never enumerated tick by tick, schedules are never enumerated and
quantities are never expanded unit by unit.
"""

from __future__ import annotations

import bisect

STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_ERROR = "error"

__all__ = [
    "reconcile_counts",
    "STATUS_OK",
    "STATUS_INVALID",
    "STATUS_ERROR",
]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value):
    return isinstance(value, str) and value != ""


def _invalid(errors):
    return {"status": STATUS_INVALID, "errors": errors}


def _error(code, message, **extra):
    payload = {"code": code, "message": message}
    payload.update(extra)
    return {"status": STATUS_ERROR, "error": payload}


# ---------------------------------------------------------------------------
# a single journey: route geometry and feasible-position intervals
# ---------------------------------------------------------------------------


class _Journey:
    __slots__ = (
        "input_index",
        "journey_id",
        "material_id",
        "quantity",
        "route",
        "windows",
        "k",
        "earliest",
        "latest",
        "occ",
    )

    def __init__(self, input_index, journey_id, material_id, quantity, route, windows):
        self.input_index = input_index
        self.journey_id = journey_id
        self.material_id = material_id
        self.quantity = quantity
        self.route = route
        self.windows = windows
        self.k = len(windows)
        # earliest[i]: earliest time of hop i (0-based), prefix maxima of starts
        earliest = []
        cur = None
        for start, _ in windows:
            cur = start if cur is None else (start if start > cur else cur)
            earliest.append(cur)
        self.earliest = earliest
        # latest[i]: latest time of hop i, suffix minima of ends
        latest = [0] * self.k
        cur = None
        for i in range(self.k - 1, -1, -1):
            end = windows[i][1]
            cur = end if cur is None else (end if end < cur else cur)
            latest[i] = cur
        self.latest = latest
        occ = {}
        for position, warehouse in enumerate(route):
            if warehouse is not None:
                occ.setdefault(warehouse, []).append(position)
        self.occ = occ

    def infeasible_at(self):
        """Return the first hop index (0-based) whose window cannot be met."""
        for i in range(self.k):
            if self.earliest[i] > self.latest[i]:
                return i
        return None

    def interval(self, t):
        """Feasible numbers of completed hops at instant ``t`` as ``[L, P]``."""
        # latest[i] <= t counts hops that must already have happened by t;
        # earliest[i] <= t counts hops that may already have happened by t.
        return bisect.bisect_right(self.latest, t), bisect.bisect_right(
            self.earliest, t
        )


# ---------------------------------------------------------------------------
# extremal contribution of one journey
# ---------------------------------------------------------------------------


def _extreme(journey, queries, want_max):
    """Extreme of ``sum_i weight_i(hops_completed at t_i)`` over all timings.

    ``queries`` is a list of ``(t, weights)`` sorted by increasing ``t`` with
    strictly distinct ``t`` and ``weights`` a ``{position: coefficient}`` dict.

    For one instant the reachable hop counts form the interval
    ``[L, P] = [last hop that may still be pending, hops that may be done]``.
    The dynamic program is

        value_i(p) = weight_i(p) + opt_{p' <= p} value_{i-1}(p')

    over that interval; the running optimum is kept as a prefix scan, so each
    instant costs ``O(P - L + 1)`` hop positions.  Only hop positions are
    visited — neither the time windows nor the schedules are enumerated.
    """
    items = [(t, w) for t, w in queries if w]
    if not items:
        return 0
    prev_values = None
    prev_low = 0
    prev_high = 0
    for t, weights in items:
        low, high = journey.interval(t)
        if prev_values is None:
            current = [weights.get(pos, 0) for pos in range(low, high + 1)]
        else:
            prefix = [0] * len(prev_values)
            best = None
            if want_max:
                for idx, value in enumerate(prev_values):
                    if best is None or value > best:
                        best = value
                    prefix[idx] = best
            else:
                for idx, value in enumerate(prev_values):
                    if best is None or value < best:
                        best = value
                    prefix[idx] = best
            current = []
            for pos in range(low, high + 1):
                # ``low`` never decreases, so a predecessor always exists
                cutoff = pos if pos < prev_high else prev_high
                current.append(prefix[cutoff - prev_low] + weights.get(pos, 0))
        prev_values = current
        prev_low, prev_high = low, high
    return max(prev_values) if want_max else min(prev_values)


# ---------------------------------------------------------------------------
# positional membership helpers (routes never repeat a warehouse back-to-back)
# ---------------------------------------------------------------------------


def _first_member(low, high, positions, want_member):
    """Smallest position in ``[low, high]`` that is (not) in ``positions``."""
    if low > high:
        return None
    idx = bisect.bisect_left(positions, low)
    if want_member:
        if idx < len(positions) and positions[idx] <= high:
            return positions[idx]
        return None
    if idx >= len(positions) or positions[idx] > low:
        return low
    # ``low`` itself is a member; adjacent route entries differ, so no two
    # positions in ``positions`` are consecutive and ``low + 1`` is free.
    return low + 1 if low + 1 <= high else None


def _has_member(low, high, positions, want_member):
    if low > high:
        return False
    if want_member:
        idx = bisect.bisect_left(positions, low)
        return idx < len(positions) and positions[idx] <= high
    inside = bisect.bisect_right(positions, high) - bisect.bisect_left(
        positions, low
    )
    return inside < (high - low + 1)


def _has_pair(low1, high1, low2, high2, positions, want1, want2):
    """Existence of ``l1 <= l2`` with ``l1`` and ``l2`` having the wanted class."""
    first = _first_member(low1, high1, positions, want1)
    if first is None:
        return False
    return _has_member(max(low2, first), high2, positions, want2)


def _row_extreme(journey, warehouse, count_time, close_time):
    """``(min, max)`` of ``quantity * (at_close - at_count)`` for one row."""
    if count_time == close_time:
        return 0, 0
    earlier, later = (
        (count_time, close_time)
        if count_time < close_time
        else (close_time, count_time)
    )
    low1, high1 = journey.interval(earlier)
    low2, high2 = journey.interval(later)
    positions = journey.occ.get(warehouse)
    if positions is None:
        return 0, 0
    sign = journey.quantity if close_time > count_time else -journey.quantity
    low = high = None
    if _has_pair(low1, high1, low2, high2, positions, True, False):
        low = high = -sign
    if _has_pair(low1, high1, low2, high2, positions, False, True):
        low = sign if low is None else min(low, sign)
        high = sign if high is None else max(high, sign)
    if _has_pair(low1, high1, low2, high2, positions, True, True) or _has_pair(
        low1, high1, low2, high2, positions, False, False
    ):
        low = 0 if low is None else min(low, 0)
        high = 0 if high is None else max(high, 0)
    # A feasible route always admits at least one of these patterns for any
    # pair of instants: every concrete timing yields one of them.
    assert low is not None
    return low, high


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _parse_row(raw, index, errors):
    where = "counts[%d]" % index
    if not isinstance(raw, dict):
        errors.append(
            {
                "code": "invalid_field",
                "message": "%s: must be an object" % where,
                "input_index": index,
            }
        )
        return None
    ok = True
    for field in ("warehouse_id", "material_id"):
        if not _nonempty_str(raw.get(field)):
            errors.append(
                {
                    "code": "invalid_field",
                    "message": "%s.%s: must be a non-empty string" % (where, field),
                    "input_index": index,
                }
            )
            ok = False
    for field in ("count_time",):
        if not _is_int(raw.get(field)):
            errors.append(
                {
                    "code": "invalid_field",
                    "message": "%s.%s: must be an integer" % (where, field),
                    "input_index": index,
                }
            )
            ok = False
    for field in ("counted", "book"):
        value = raw.get(field)
        if not _is_int(value) or value < 0:
            errors.append(
                {
                    "code": "invalid_field",
                    "message": "%s.%s: must be a non-negative integer"
                    % (where, field),
                    "input_index": index,
                }
            )
            ok = False
    if not ok:
        return None
    return {
        "input_index": index,
        "warehouse_id": raw["warehouse_id"],
        "material_id": raw["material_id"],
        "count_time": raw["count_time"],
        "counted": raw["counted"],
        "book": raw["book"],
    }


def _parse_journey(raw, index, errors):
    where = "journeys[%d]" % index
    if not isinstance(raw, dict):
        errors.append(
            {
                "code": "invalid_field",
                "message": "%s: must be an object" % where,
                "input_index": index,
            }
        )
        return None

    def bad(message):
        errors.append(
            {
                "code": "invalid_field",
                "message": "%s: %s" % (where, message),
                "input_index": index,
            }
        )

    ok = True
    journey_id = raw.get("journey_id")
    if not _nonempty_str(journey_id):
        bad("journey_id must be a non-empty string")
        ok = False
    if not _nonempty_str(raw.get("material_id")):
        bad("material_id must be a non-empty string")
        ok = False
    quantity = raw.get("quantity")
    if not _is_int(quantity) or quantity < 1:
        bad("quantity must be a positive integer")
        ok = False

    route = raw.get("route")
    if not isinstance(route, list) or len(route) < 2:
        bad("route must be a list of at least two entries")
        route = None
        ok = False
    else:
        for position, entry in enumerate(route):
            if entry is not None and not _nonempty_str(entry):
                bad(
                    "route[%d] must be a warehouse id (non-empty string) or null"
                    % position
                )
                ok = False
        if ok:
            for position in range(1, len(route)):
                if route[position] == route[position - 1]:
                    bad(
                        "route[%d] and route[%d] are the same stop"
                        % (position - 1, position)
                    )
                    ok = False
                    break

    windows = raw.get("windows")
    if not isinstance(windows, list):
        bad("windows must be a list of inclusive integer intervals")
        windows = None
        ok = False
    else:
        if isinstance(route, list) and len(windows) != len(route) - 1:
            bad(
                "windows has %d entries but the route has %d hops"
                % (len(windows), len(route) - 1)
            )
            ok = False
        for position, window in enumerate(windows):
            valid = (
                isinstance(window, list)
                and len(window) == 2
                and _is_int(window[0])
                and _is_int(window[1])
                and window[0] <= window[1]
            )
            if not valid:
                bad(
                    "windows[%d] must be [earliest, latest] with integer "
                    "earliest <= latest" % position
                )
                ok = False

    if not ok:
        return None
    return _Journey(
        index,
        journey_id,
        raw["material_id"],
        quantity,
        list(route),
        [(window[0], window[1]) for window in windows],
    )


def _validate(payload):
    """Return ``(close_time, rows, journeys, errors)``; lists empty on error."""
    errors = []
    if not isinstance(payload, dict):
        return None, [], [], [
            {
                "code": "invalid_field",
                "message": "payload must be an object",
                "input_index": None,
            }
        ]

    close_time = payload.get("close_time")
    if not _is_int(close_time):
        errors.append(
            {
                "code": "invalid_field",
                "message": "close_time: must be an integer",
                "input_index": None,
            }
        )

    raw_counts = payload.get("counts")
    if not isinstance(raw_counts, list):
        errors.append(
            {
                "code": "invalid_field",
                "message": "counts: must be a list of counted rows",
                "input_index": None,
            }
        )
        raw_counts = []
    raw_journeys = payload.get("journeys")
    if not isinstance(raw_journeys, list):
        errors.append(
            {
                "code": "invalid_field",
                "message": "journeys: must be a list of journeys",
                "input_index": None,
            }
        )
        raw_journeys = []

    if errors:
        return close_time, [], [], errors

    rows = []
    for index, raw in enumerate(raw_counts):
        row = _parse_row(raw, index, errors)
        if row is not None:
            rows.append(row)
    journeys = []
    for index, raw in enumerate(raw_journeys):
        journey = _parse_journey(raw, index, errors)
        if journey is not None:
            journeys.append(journey)

    combinations = {}
    for row in rows:
        key = (row["warehouse_id"], row["material_id"])
        combinations.setdefault(key, []).append(row["input_index"])
    for (warehouse, material), indices in combinations.items():
        if len(indices) > 1:
            errors.append(
                {
                    "code": "duplicate_count_combination",
                    "message": (
                        "warehouse %r / material %r is counted more than once "
                        "(counts rows %s)"
                        % (warehouse, material, sorted(indices))
                    ),
                    "warehouse_id": warehouse,
                    "material_id": material,
                    "input_indices": sorted(indices),
                }
            )

    ids = {}
    for journey in journeys:
        ids.setdefault(journey.journey_id, []).append(journey.input_index)
    for journey_id, indices in ids.items():
        if len(indices) > 1:
            errors.append(
                {
                    "code": "duplicate_journey_id",
                    "message": "journey_id %r is used more than once (journeys rows %s)"
                    % (journey_id, sorted(indices)),
                    "journey_id": journey_id,
                    "input_indices": sorted(indices),
                }
            )

    if errors:
        return close_time, [], [], errors
    return close_time, rows, journeys, []


# ---------------------------------------------------------------------------
# public entry
# ---------------------------------------------------------------------------


def reconcile_counts(payload):
    """Reconcile physical counts to ``close_time``; see the module docstring.

    Returns ``{"status": "ok", "rows": [...], "material_totals": [...]}`` on
    success, ``{"status": "invalid", "errors": [...]}`` for malformed input and
    ``{"status": "error", "error": {...}}`` when a journey has no feasible set
    of hop times.  No partial report is emitted on failure.
    """
    close_time, rows, journeys, errors = _validate(payload)
    if errors:
        return _invalid(errors)

    for journey in journeys:
        hop = journey.infeasible_at()
        if hop is not None:
            return _error(
                "infeasible_route",
                (
                    "journey %r has no non-decreasing hop times inside its "
                    "windows (hop %d cannot start early enough and still finish "
                    "by its latest end)" % (journey.journey_id, hop + 1)
                ),
                journey_id=journey.journey_id,
                input_index=journey.input_index,
                material_id=journey.material_id,
                route=journey.route,
                windows=[list(window) for window in journey.windows],
            )

    journeys_by_material = {}
    # journeys that may ever touch a (material, warehouse); a row only needs
    # those, so unrelated movements do not cost time per row
    journeys_by_stop = {}
    for journey in journeys:
        journeys_by_material.setdefault(journey.material_id, []).append(journey)
        for warehouse in journey.occ:
            journeys_by_stop.setdefault(
                (journey.material_id, warehouse), []
            ).append(journey)

    # -- per counted warehouse/material ------------------------------------
    row_result = {}
    for row in rows:
        warehouse = row["warehouse_id"]
        material = row["material_id"]
        counted = row["counted"]
        book = row["book"]
        low = high = counted
        for journey in journeys_by_stop.get((material, warehouse), ()):
            delta_low, delta_high = _row_extreme(
                journey, warehouse, row["count_time"], close_time
            )
            low += delta_low
            high += delta_high
        row_result[(material, warehouse)] = {
            "warehouse_id": warehouse,
            "material_id": material,
            "count_time": row["count_time"],
            "counted": counted,
            "book": book,
            "reconciled_min": low,
            "reconciled_max": high,
            "difference_min": low - book,
            "difference_max": high - book,
        }

    # -- per material, summed over the warehouses actually counted ---------
    rows_by_material = {}
    for row in rows:
        rows_by_material.setdefault(row["material_id"], []).append(row)

    total_result = {}
    for material in rows_by_material:
        material_rows = rows_by_material[material]
        counted_total = sum(row["counted"] for row in material_rows)
        book_total = sum(row["book"] for row in material_rows)
        warehouses = {row["warehouse_id"] for row in material_rows}
        rows_by_warehouse = {}
        for row in material_rows:
            rows_by_warehouse.setdefault(row["warehouse_id"], []).append(row)
        low = high = counted_total
        for journey in journeys_by_material.get(material, ()):
            quantity = journey.quantity
            weights_by_time = {}
            # goods present at closing in a counted warehouse count once; a
            # counted warehouse its route never visits needs no weight
            closing = weights_by_time.setdefault(close_time, {})
            for warehouse, positions in journey.occ.items():
                if warehouse in warehouses:
                    for position in positions:
                        closing[position] = closing.get(position, 0) + quantity
            for warehouse, positions in journey.occ.items():
                for row in rows_by_warehouse.get(warehouse, ()):
                    delta = weights_by_time.setdefault(row["count_time"], {})
                    for position in positions:
                        delta[position] = delta.get(position, 0) - quantity
            queries = [
                (time, {pos: coef for pos, coef in weights.items() if coef})
                for time, weights in sorted(weights_by_time.items())
            ]
            low += _extreme(journey, queries, False)
            high += _extreme(journey, queries, True)
        total_result[material] = {
            "material_id": material,
            "counted_total": counted_total,
            "book_total": book_total,
            "reconciled_min": low,
            "reconciled_max": high,
            "difference_min": low - book_total,
            "difference_max": high - book_total,
        }

    return {
        "status": STATUS_OK,
        "close_time": close_time,
        "rows": [row_result[key] for key in sorted(row_result)],
        "material_totals": [total_result[key] for key in sorted(total_result)],
    }
