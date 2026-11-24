"""Naive reference for ``reconcile-counts``.

Enumerates every feasible hop-time tuple of every journey, takes the Cartesian
product across the journeys of one material and evaluates the reconciliation
literally.  Only suitable for tiny instances (a few hops per journey, short
windows).
"""


def _schedules(windows):
    """All non-decreasing hop-time tuples inside the given windows."""
    out = []

    def walk(index, previous, current):
        if index == len(windows):
            out.append(tuple(current))
            return
        start, end = windows[index]
        for value in range(max(start, previous), end + 1):
            current.append(value)
            walk(index + 1, value, current)
            current.pop()

    walk(0, -(1 << 60), [])
    return out


def _station(route, times, instant):
    """Warehouse occupied at ``instant`` for one concrete schedule."""
    completed = 0
    for hop_time in times:
        if hop_time <= instant:
            completed += 1
    return route[completed]


def ref_reconcile(payload):
    """Return ``(rows, totals)`` for a valid, feasible payload.

    ``rows`` maps ``(material_id, warehouse_id)`` to
    ``{"reconciled_min", "reconciled_max", "difference_min", "difference_max"}``.
    ``totals`` maps ``material_id`` to ``counted_total``, ``book_total`` and the
    same four reconciliation bounds.
    """
    close_time = payload["close_time"]
    rows = payload["counts"]
    journeys = payload["journeys"]

    schedules = {
        journey["journey_id"]: _schedules(
            [(window[0], window[1]) for window in journey["windows"]]
        )
        for journey in journeys
    }
    by_material = {}
    for journey in journeys:
        by_material.setdefault(journey["material_id"], []).append(journey)
    # materials that were counted but have no journeys still get a row
    for row in rows:
        by_material.setdefault(row["material_id"], [])

    row_bounds = {}
    total_bounds = {}
    for material, material_journeys in by_material.items():
        material_rows = [row for row in rows if row["material_id"] == material]
        if not material_rows:
            continue
        options = [
            schedules[journey["journey_id"]] for journey in material_journeys
        ]
        for combination in _product(options):
            reconciled = {
                row["warehouse_id"]: row["counted"] for row in material_rows
            }
            for journey, times in zip(material_journeys, combination):
                at_close = _station(journey["route"], times, close_time)
                for row in material_rows:
                    warehouse = row["warehouse_id"]
                    at_count = _station(
                        journey["route"], times, row["count_time"]
                    )
                    reconciled[warehouse] += journey["quantity"] * (
                        (1 if at_close == warehouse else 0)
                        - (1 if at_count == warehouse else 0)
                    )
            for row in material_rows:
                key = (material, row["warehouse_id"])
                value = reconciled[row["warehouse_id"]]
                _extend(row_bounds, key, value)
            _extend(total_bounds, material, sum(reconciled.values()))

    result_rows = {}
    for (material, warehouse), (low, high) in row_bounds.items():
        book = next(
            row["book"]
            for row in rows
            if row["material_id"] == material and row["warehouse_id"] == warehouse
        )
        result_rows[(material, warehouse)] = {
            "reconciled_min": low,
            "reconciled_max": high,
            "difference_min": low - book,
            "difference_max": high - book,
        }

    result_totals = {}
    for material, (low, high) in total_bounds.items():
        material_rows = [row for row in rows if row["material_id"] == material]
        book_total = sum(row["book"] for row in material_rows)
        result_totals[material] = {
            "counted_total": sum(row["counted"] for row in material_rows),
            "book_total": book_total,
            "reconciled_min": low,
            "reconciled_max": high,
            "difference_min": low - book_total,
            "difference_max": high - book_total,
        }
    return result_rows, result_totals


def _product(options):
    if not options:
        yield ()
        return
    for head in options[0]:
        for tail in _product(options[1:]):
            yield (head,) + tail


def _extend(bounds, key, value):
    low, high = bounds.get(key, (value, value))
    bounds[key] = (min(low, value), max(high, value))
