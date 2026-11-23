"""Logical pagination of a ledger (领用台账) for a page-at-a-time archiving system.

Model
-----
The input is an ordered list of detail lines.  Each detail carries:

* ``group``     – first-level group id; every group occurs as ONE contiguous
                  interval of the detail sequence;
* ``qty``       – exact signed quantity (used for per-page control totals);
* ``units``     – row units occupied by the line, an integer >= 1;
* ``citations`` – ids of batch citations the line references; the citation
                  catalog declares a non-negative integer unit cost per id.

A page is a contiguous, non-empty slice of the details.  Its unit cost is

    sum(detail units)                    original rows
  + 2 * (#distinct groups on the page)   one group header + one control line
  + sum(units of distinct citations)     each citation sent once per page,
                                         no matter how many groups use it
  + (1 if the page continues a group from the previous page, else 0)
  + (1 if the page leaves a group to continue on the next page, else 0)

and must not exceed the fixed page capacity.  Continuation markers are always
computed against the *original full ledger* — also for the prefix pages of an
infeasible instance, which are reported but not treated as delivered.

Groups listed in ``unbreakable_groups`` may not straddle a page boundary, i.e.
no boundary may fall strictly inside their interval.

Objective: among all legal paginations minimise the page count; ties are broken
by the lexicographically smallest vector of page-end boundary positions.

If the instance cannot be paged at all, the solver reports the maximal boundary
reachable from the start by any legal pagination, one legal path to it, and the
blocking evidence.  Invalid input is reported separately from valid-but-
infeasible input.

Algorithm (no dense position-pair table is ever built)
------------------------------------------------------
1. ``max_end[i]`` = largest page end affordable from start ``i``.  Page cost is
   monotone non-decreasing in the end and non-increasing in the start, so a
   single two-pointer sweep with an incremental window (unit sum, group count,
   citation multiplicity counts) yields all ``max_end`` in O(n + citations).
2. ``level[p]`` = min pages to cover ``[p, n)``.  Because page cost is monotone
   in the start, ``level`` is non-increasing over allowed boundaries, hence
   ``level[p] = 1 + level[clamp(max_end[p])]`` where ``clamp`` snaps down to
   the nearest allowed boundary.  One backward pass, O(n).
3. Feasible iff ``level[0]`` is finite.  The lexicographically smallest
   boundary vector is then greedy: from a position of level ``r`` the next
   boundary is the first allowed position whose level drops to ``r - 1``
   (precomputed threshold table ``L``), O(n).
4. Infeasible: iterating the furthest-jump map ``p -> clamp(max_end[p])`` from
   0 reaches a fixed point ``M*`` which is exactly the maximal coverable
   boundary; the iterates form a legal path to it.  Blocking evidence is either
   "the single next detail does not fit" or "the unbreakable group starting at
   ``M*`` does not fit on one page".

Total time O(n + citation references), memory O(n).  Only logical pages are
produced — no typesetting, no PDF, no network archiving.
"""

from __future__ import annotations

import math
from decimal import Decimal
from fractions import Fraction

HEADER_UNITS = 1
CONTROL_UNITS = 1
MARKER_UNITS = 1

STATUS_OK = "ok"
STATUS_INFEASIBLE = "infeasible"
STATUS_INVALID = "invalid"

__all__ = [
    "paginate",
    "HEADER_UNITS",
    "CONTROL_UNITS",
    "MARKER_UNITS",
    "STATUS_OK",
    "STATUS_INFEASIBLE",
    "STATUS_INVALID",
]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


class _Norm:
    """Validated, solver-ready input."""

    __slots__ = (
        "n",
        "groups",
        "qtys",
        "units",
        "cits",
        "catalog",
        "catalog_order",
        "unbreakable",
        "capacity",
        "group_intervals",
        "group_order",
    )

    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_qty(value, idx, errors):
    where = "details[%d].qty" % idx
    if value is None or isinstance(value, bool):
        errors.append("%s: must be an exact signed number" % where)
        return Fraction(0)
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Fraction):
        return value
    if isinstance(value, Decimal):
        return Fraction(value)
    if isinstance(value, float):
        if math.isfinite(value):
            return Fraction(str(value))
        errors.append("%s: must be finite" % where)
        return Fraction(0)
    if isinstance(value, str):
        try:
            return Fraction(value.strip())
        except (ValueError, ZeroDivisionError):
            errors.append("%s: cannot parse %r as an exact number" % (where, value))
            return Fraction(0)
    errors.append("%s: unsupported type %s" % (where, type(value).__name__))
    return Fraction(0)


def _validate(payload):
    """Return ``(norm, [])`` on success or ``(None, [reasons])`` on invalid input."""
    errors = []
    if not isinstance(payload, dict):
        return None, ["payload must be an object"]

    capacity = payload.get("capacity")
    if not _is_int(capacity) or capacity < 1:
        errors.append("capacity: must be an integer >= 1")
        capacity = None

    catalog = {}
    catalog_order = []
    cat_in = payload.get("citation_catalog", {})
    if not isinstance(cat_in, dict):
        errors.append("citation_catalog: must be an object mapping citation id to units")
        cat_in = {}
    for cid, units in cat_in.items():
        if not _is_int(units) or units < 0:
            errors.append(
                "citation_catalog[%r]: units must be a non-negative integer" % (cid,)
            )
        else:
            catalog[cid] = units
            catalog_order.append(cid)

    groups = []
    qtys = []
    units = []
    cits = []
    det_in = payload.get("details")
    if not isinstance(det_in, list) or len(det_in) == 0:
        errors.append("details: must be a non-empty list")
        det_in = []
    for idx, detail in enumerate(det_in):
        if not isinstance(detail, dict):
            errors.append("details[%d]: must be an object" % idx)
            groups.append(None)
            qtys.append(Fraction(0))
            units.append(1)
            cits.append(())
            continue
        gid = detail.get("group")
        if isinstance(gid, bool) or not isinstance(gid, (str, int)):
            errors.append("details[%d].group: must be a string or integer id" % idx)
            gid = None
        groups.append(gid)
        qtys.append(_parse_qty(detail.get("qty"), idx, errors))
        u = detail.get("units")
        if not _is_int(u) or u < 1:
            errors.append("details[%d].units: must be an integer >= 1" % idx)
            u = 1
        units.append(u)
        cl = detail.get("citations", [])
        if not isinstance(cl, (list, tuple)):
            errors.append("details[%d].citations: must be a list" % idx)
            cl = []
        seen = set()
        dedup = []
        for c in cl:
            if c not in catalog:
                errors.append(
                    "details[%d].citations: unknown citation id %r" % (idx, c)
                )
            elif c not in seen:
                seen.add(c)
                dedup.append(c)
        cits.append(tuple(dedup))

    # group contiguity: each group id must occur as one contiguous interval
    group_order = []
    intervals = {}
    prev = None
    have_prev = False
    for idx, gid in enumerate(groups):
        if gid is None:
            have_prev = False
            prev = None
            continue
        if not have_prev or gid != prev:
            if gid in intervals:
                errors.append(
                    "group %r: occurs in several disjoint intervals; "
                    "a group must be one contiguous interval" % (gid,)
                )
            else:
                intervals[gid] = [idx, idx + 1]
                group_order.append(gid)
            prev = gid
            have_prev = True
        else:
            intervals[gid][1] = idx + 1

    unbreakable = set()
    unb_in = payload.get("unbreakable_groups", [])
    if not isinstance(unb_in, (list, tuple, set)):
        errors.append("unbreakable_groups: must be a list of group ids")
    else:
        for gid in unb_in:
            if gid not in intervals:
                errors.append("unbreakable_groups: unknown group %r" % (gid,))
            else:
                unbreakable.add(gid)

    if errors:
        return None, errors

    norm = _Norm(
        n=len(groups),
        groups=groups,
        qtys=qtys,
        units=units,
        cits=cits,
        catalog=catalog,
        catalog_order=catalog_order,
        unbreakable=unbreakable,
        capacity=capacity,
        group_intervals=intervals,
        group_order=group_order,
    )
    return norm, []


# ---------------------------------------------------------------------------
# cost helpers
# ---------------------------------------------------------------------------


def _cont_from(groups, i):
    return 1 if (i > 0 and groups[i - 1] == groups[i]) else 0


def _cont_to(groups, n, j):
    return 1 if (j < n and groups[j - 1] == groups[j]) else 0


def _cost_breakdown(norm, i, j):
    """From-scratch unit cost of a page covering details [i, j)."""
    groups = norm.groups
    n = norm.n
    detail_units = sum(norm.units[i:j])
    group_count = len(set(groups[i:j]))
    page_cits = set()
    for p in range(i, j):
        page_cits.update(norm.cits[p])
    citation_units = sum(norm.catalog[c] for c in page_cits)
    cf = _cont_from(groups, i)
    ct = _cont_to(groups, n, j)
    total = (
        detail_units
        + (HEADER_UNITS + CONTROL_UNITS) * group_count
        + citation_units
        + cf * MARKER_UNITS
        + ct * MARKER_UNITS
    )
    return {
        "detail_units": detail_units,
        "group_count": group_count,
        "header_control_units": (HEADER_UNITS + CONTROL_UNITS) * group_count,
        "citation_units": citation_units,
        "continuation_from_previous": cf,
        "continuation_to_next": ct,
        "total_units": total,
    }


def _allowed_boundaries(norm):
    """``allowed[b]``: a page boundary may sit before detail ``b``."""
    n = norm.n
    groups = norm.groups
    allowed = [True] * (n + 1)
    for b in range(1, n):
        if groups[b - 1] == groups[b] and groups[b] in norm.unbreakable:
            allowed[b] = False
    return allowed


# ---------------------------------------------------------------------------
# two-pointer sweep: max_end[i] = largest affordable page end from start i
# ---------------------------------------------------------------------------


def _max_ends(norm, stats):
    n = norm.n
    groups = norm.groups
    capacity = norm.capacity
    units = norm.units
    dcits = norm.cits
    catalog = norm.catalog

    max_end = [0] * n
    counts = {}  # citation id -> multiplicity inside the window
    sum_units = 0
    num_groups = 0
    cit_units = 0
    j = 0
    for i in range(n):
        if j < i:
            j = i  # window is empty; incremental structures are empty too
        cf = _cont_from(groups, i)
        while j < n:
            stats["window_probes"] += 1
            u = units[j]
            new_group = 1 if (j == i or groups[j] != groups[j - 1]) else 0
            add_cit = 0
            for c in dcits[j]:
                if counts.get(c, 0) == 0:
                    add_cit += catalog[c]
            ct = _cont_to(groups, n, j + 1)
            cost = (
                (sum_units + u)
                + (HEADER_UNITS + CONTROL_UNITS) * (num_groups + new_group)
                + (cit_units + add_cit)
                + (cf + ct) * MARKER_UNITS
            )
            if cost > capacity:
                break
            sum_units += u
            num_groups += new_group
            cit_units += add_cit
            for c in dcits[j]:
                counts[c] = counts.get(c, 0) + 1
            j += 1
            stats["window_extensions"] += 1
        max_end[i] = j
        # slide the window's left edge past detail i
        if i < j:
            stats["window_removals"] += 1
            sum_units -= units[i]
            if i + 1 >= j or groups[i + 1] != groups[i]:
                num_groups -= 1
            for c in dcits[i]:
                counts[c] -= 1
                if counts[c] == 0:
                    del counts[c]
                    cit_units -= catalog[c]
    return max_end


# ---------------------------------------------------------------------------
# page assembly
# ---------------------------------------------------------------------------


def _fmt_qty(q):
    return str(q.numerator) if q.denominator == 1 else "%d/%d" % (q.numerator, q.denominator)


def _assemble_pages(norm, boundaries):
    """Build the logical pages for a boundary path (list of page ends)."""
    n = norm.n
    groups = norm.groups
    pages = []
    start = 0
    for page_index, end in enumerate(boundaries):
        cont_from = start > 0 and groups[start - 1] == groups[start]
        cont_to = end < n and groups[end - 1] == groups[end]
        lines = []
        units_used = 0
        if cont_from:
            units_used += MARKER_UNITS
            lines.append(
                {
                    "type": "continuation",
                    "direction": "from_previous",
                    "group": groups[start],
                }
            )
        if cont_to:
            units_used += MARKER_UNITS
        segments = []
        page_cits = set()
        p = start
        while p < end:
            q = p + 1
            while q < end and groups[q] == groups[p]:
                q += 1
            gid = groups[p]
            lines.append({"type": "group_header", "group": gid})
            units_used += HEADER_UNITS
            total = Fraction(0)
            for r in range(p, q):
                total += norm.qtys[r]
                units_used += norm.units[r]
                page_cits.update(norm.cits[r])
                lines.append(
                    {
                        "type": "detail",
                        "index": r,
                        "group": gid,
                        "qty": _fmt_qty(norm.qtys[r]),
                        "units": norm.units[r],
                        "citations": list(norm.cits[r]),
                    }
                )
            lines.append(
                {"type": "control_total", "group": gid, "qty": _fmt_qty(total)}
            )
            units_used += CONTROL_UNITS
            segments.append(
                {
                    "group": gid,
                    "detail_start": p,
                    "detail_end": q,
                    "detail_count": q - p,
                    "control_total": _fmt_qty(total),
                }
            )
            p = q
        page_citations = [c for c in norm.catalog_order if c in page_cits]
        for c in page_citations:
            units_used += norm.catalog[c]
            lines.append({"type": "citation", "id": c, "units": norm.catalog[c]})
        if cont_to:
            lines.append(
                {
                    "type": "continuation",
                    "direction": "to_next",
                    "group": groups[end - 1],
                }
            )
        pages.append(
            {
                "index": page_index,
                "detail_start": start,
                "detail_end": end,
                "detail_count": end - start,
                "continues_from_previous": cont_from,
                "continues_to_next": cont_to,
                "groups": segments,
                "citations": page_citations,
                "lines": lines,
                "line_count": len(lines),
                "units_used": units_used,
                "capacity": norm.capacity,
                "headroom": norm.capacity - units_used,
            }
        )
        start = end
    return pages


# ---------------------------------------------------------------------------
# blocking evidence
# ---------------------------------------------------------------------------


def _blocking_evidence(norm, boundary, max_end_at_boundary):
    n = norm.n
    groups = norm.groups
    if max_end_at_boundary == boundary:
        return {
            "kind": "single_detail_exceeds_capacity",
            "at_boundary": boundary,
            "detail_index": boundary,
            "detail": {
                "group": groups[boundary],
                "units": norm.units[boundary],
                "citations": list(norm.cits[boundary]),
            },
            "page_cost_if_placed": _cost_breakdown(norm, boundary, boundary + 1),
            "capacity": norm.capacity,
        }
    gid = groups[boundary]
    start, end = norm.group_intervals[gid]
    return {
        "kind": "unbreakable_group_exceeds_capacity",
        "at_boundary": boundary,
        "group": gid,
        "group_interval": [start, end],
        "max_end_affordable": max_end_at_boundary,
        "page_cost_through_group_end": _cost_breakdown(norm, boundary, end),
        "capacity": norm.capacity,
    }


# ---------------------------------------------------------------------------
# solver
# ---------------------------------------------------------------------------


def _solve(norm):
    n = norm.n
    groups = norm.groups
    stats = {"window_probes": 0, "window_extensions": 0, "window_removals": 0}

    allowed = _allowed_boundaries(norm)
    prev_allowed = [0] * (n + 1)
    last = 0
    for x in range(n + 1):
        if allowed[x]:
            last = x
        prev_allowed[x] = last

    max_end = _max_ends(norm, stats)

    # level[p] = min number of pages covering [p, n); INF = unreachable.
    INF = n + 1
    level = [INF] * (n + 1)
    level[n] = 0
    for i in range(n - 1, -1, -1):
        if not allowed[i]:
            continue
        nxt = prev_allowed[max_end[i]]
        if nxt > i and level[nxt] < INF:
            level[i] = level[nxt] + 1

    if level[0] < INF:
        k = level[0]
        # L[r] = first allowed boundary whose level is <= r (levels are
        # non-increasing along allowed boundaries).
        thresholds = [0] * (k + 1)
        cur = k
        for p in range(n + 1):
            if not allowed[p]:
                continue
            lv = level[p]
            if lv >= INF:
                continue
            while cur >= lv and cur >= 0:
                thresholds[cur] = p
                cur -= 1
        boundaries = []
        p = 0
        for r in range(k, 0, -1):
            p = thresholds[r - 1]
            boundaries.append(p)
        stats["path_steps"] = k
        return {
            "status": STATUS_OK,
            "page_count": k,
            "boundary_vector": boundaries[:-1],
            "page_end_boundaries": boundaries,
            "detail_count": n,
            "capacity": norm.capacity,
            "pages": _assemble_pages(norm, boundaries),
            "stats": stats,
        }

    # infeasible: furthest-reach iteration to the fixed point M*
    boundaries = []
    m = 0
    while m < n:
        nxt = prev_allowed[max_end[m]]
        if nxt <= m:
            break
        boundaries.append(nxt)
        m = nxt
    stats["path_steps"] = len(boundaries)
    return {
        "status": STATUS_INFEASIBLE,
        "max_covered_boundary": m,
        "covered_details": m,
        "total_details": n,
        "capacity": norm.capacity,
        "prefix": {
            "delivered": False,
            "note": (
                "prefix pages are computed with continuation markers against "
                "the original full ledger and are not treated as delivered"
            ),
            "boundaries": boundaries,
            "pages": _assemble_pages(norm, boundaries),
        },
        "blocking_evidence": _blocking_evidence(norm, m, max_end[m]),
        "stats": stats,
    }


def paginate(payload):
    """Paginate a ledger into logical pages.

    ``payload`` is a dict with keys ``capacity`` (int >= 1), ``details``
    (non-empty list of ``{group, qty, units, citations?}``),
    ``citation_catalog`` (dict id -> non-negative int units) and
    ``unbreakable_groups`` (list of group ids).  Returns a result dict with
    ``status`` one of ``"ok"`` / ``"infeasible"`` / ``"invalid"``.
    """
    norm, errors = _validate(payload)
    if errors:
        return {"status": STATUS_INVALID, "reasons": errors}
    return _solve(norm)
