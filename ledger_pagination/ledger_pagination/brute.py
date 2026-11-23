"""Exhaustive reference solver used to cross-check the fast solver on small inputs.

Everything here is deliberately naive: page costs are recomputed from scratch
and all boundary combinations are explored with O(n^2) dynamic programming.
Only suitable for tiny instances.
"""

from __future__ import annotations

from .core import _allowed_boundaries, _cost_breakdown, _validate


def brute_paginate(payload):
    """Return a result dict comparable to :func:`ledger_pagination.paginate`.

    ok:         {"status": "ok", "page_count": k, "boundaries": [ends...]}
    infeasible: {"status": "infeasible", "max_covered_boundary": m,
                 "path": [ends...]}
    invalid:    {"status": "invalid", "reasons": [...]}
    """
    norm, errors = _validate(payload)
    if errors:
        return {"status": "invalid", "reasons": errors}

    n = norm.n
    capacity = norm.capacity
    allowed = _allowed_boundaries(norm)
    cost = [[0] * (n + 1) for _ in range(n + 1)]
    for i in range(n):
        for j in range(i + 1, n + 1):
            cost[i][j] = _cost_breakdown(norm, i, j)["total_units"]

    # reachability of prefixes [0, j) by any legal pagination
    reach = [False] * (n + 1)
    parent = [-1] * (n + 1)
    reach[0] = True
    for i in range(n + 1):
        if not reach[i] or not allowed[i]:
            continue
        for j in range(i + 1, n + 1):
            if allowed[j] and cost[i][j] <= capacity and not reach[j]:
                reach[j] = True
                parent[j] = i

    max_cov = max(j for j in range(n + 1) if reach[j])
    if not reach[n]:
        path = []
        cur = max_cov
        while cur != 0:
            path.append(cur)
            cur = parent[cur]
        path.reverse()
        return {
            "status": "infeasible",
            "max_covered_boundary": max_cov,
            "path": path,
        }

    # min pages, then lexicographically smallest boundary vector
    best = [None] * (n + 1)
    best[0] = (0, ())
    for i in range(n + 1):
        if best[i] is None or not allowed[i]:
            continue
        pages_i, bounds_i = best[i]
        for j in range(i + 1, n + 1):
            if not allowed[j] or cost[i][j] > capacity:
                continue
            cand = (pages_i + 1, bounds_i + (j,))
            if best[j] is None or cand < best[j]:
                best[j] = cand
    pages, bounds = best[n]
    return {"status": "ok", "page_count": pages, "boundaries": list(bounds)}
