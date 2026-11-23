"""Naive reference implementation of ``trace_sources`` for cross-checking.

Deliberately dumb: stock is kept as an explicit list of ``[receipt, qty]``
chunks, FIFO consumption walks and copies the list, and no compact structure is
used.  Only suitable for tiny instances; the production implementation in
``ledger_pagination/sources.py`` must agree with it.

The reference re-derives version selection from the payload instead of calling
into the production module, so selection and replay are both cross-checked.
"""

from __future__ import annotations

import json
from collections import defaultdict

CONTENT_FIELDS = (
    "type",
    "business_time",
    "material_id",
    "warehouse_id",
    "quantity",
    "to_warehouse_id",
    "issue_doc_id",
)


class RefError(Exception):
    def __init__(self, code, doc_id):
        super().__init__(code)
        self.code = code
        self.doc_id = doc_id


def _canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _void(raw):
    for flag in ("void", "cancelled", "canceled", "revoked"):
        if raw.get(flag) is True:
            return True
    return raw.get("type") == "void"


def _sig(raw):
    if _void(raw):
        return ("void",)
    return tuple(
        ("absent",) if field not in raw else _canon(raw[field])
        for field in CONTENT_FIELDS
    )


def select_docs(payload):
    """Return participating docs in processing order (raises RefError)."""
    business_cutoff = payload["business_cutoff"]
    entry_cutoff = payload["entry_cutoff"]
    by_doc = defaultdict(lambda: defaultdict(list))
    for raw in payload["versions"]:
        if raw["entry_time"] > entry_cutoff:
            continue
        by_doc[raw["doc_id"]][raw["entry_time"]].append(raw)
    chosen = {}
    for doc_id, times in by_doc.items():
        for entry_time, group in times.items():
            if len({_sig(raw) for raw in group}) > 1:
                raise RefError("version_conflict", doc_id)
        entry_time = max(times)
        chosen[doc_id] = times[entry_time][0]
    docs = []
    for doc_id, raw in chosen.items():
        if _void(raw):
            continue
        if raw["business_time"] > business_cutoff:
            continue
        docs.append(
            {
                "doc_id": doc_id,
                "entry_time": raw["entry_time"],
                "type": raw["type"],
                "business_time": raw["business_time"],
                "material_id": raw["material_id"],
                "warehouse_id": raw["warehouse_id"],
                "quantity": raw["quantity"],
                "to_warehouse_id": raw.get("to_warehouse_id"),
                "issue_doc_id": raw.get("issue_doc_id"),
            }
        )
    docs.sort(key=lambda d: (d["business_time"], d["doc_id"]))
    return docs


def _sources(chunks, sign):
    totals = defaultdict(int)
    for receipt, qty in chunks:
        totals[receipt] += qty
    return [
        {"receipt_doc_id": receipt, "quantity": sign * totals[receipt]}
        for receipt in sorted(totals)
    ]


def ref_trace(payload):
    """Return ``{"status": "ok", "details": [...]}`` or an error marker."""
    docs = select_docs(payload)
    receipt_materials = {d["material_id"] for d in docs if d["type"] == "receipt"}
    participating = {d["doc_id"]: d for d in docs}
    order = {d["doc_id"]: i for i, d in enumerate(docs)}
    stock = defaultdict(list)  # (warehouse, material) -> [[receipt, qty], ...]
    issues = {}  # doc_id -> {"chunks": [[receipt, qty]], "remaining": int}
    rows = []

    def take(key, qty, doc_id):
        queue = stock[key]
        available = sum(q for _, q in queue)
        if available < qty:
            raise RefError("insufficient_stock", doc_id)
        taken = []
        need = qty
        while need:
            receipt, available_here = queue[0]
            use = min(need, available_here)
            taken.append([receipt, use])
            need -= use
            if use == available_here:
                queue.pop(0)
            else:
                queue[0] = [receipt, available_here - use]
        return taken

    for doc_id, doc in sorted(participating.items(), key=lambda kv: order[kv[0]]):
        key = (doc["warehouse_id"], doc["material_id"])
        qty = doc["quantity"]
        if doc["type"] == "receipt":
            stock[key].append([doc_id, qty])
        elif doc["type"] == "transfer":
            taken = take(key, qty, doc_id)
            stock[(doc["to_warehouse_id"], doc["material_id"])].extend(taken)
        elif doc["type"] == "issue":
            if doc["material_id"] not in receipt_materials:
                raise RefError("no_receipt_evidence", doc_id)
            taken = take(key, qty, doc_id)
            issues[doc_id] = {"chunks": [list(c) for c in taken], "remaining": qty}
            rows.append(
                {
                    "doc_id": doc_id,
                    "business_time": doc["business_time"],
                    "entry_time": doc["entry_time"],
                    "type": "issue",
                    "warehouse_id": doc["warehouse_id"],
                    "material_id": doc["material_id"],
                    "quantity": qty,
                    "sources": _sources(taken, 1),
                }
            )
        else:
            if doc["material_id"] not in receipt_materials:
                raise RefError("no_receipt_evidence", doc_id)
            ref_id = doc["issue_doc_id"]
            ref = participating.get(ref_id)
            if (
                ref is None
                or ref["type"] != "issue"
                or order[ref_id] >= order[doc_id]
                or ref["warehouse_id"] != doc["warehouse_id"]
                or ref["material_id"] != doc["material_id"]
            ):
                raise RefError("invalid_return_reference", doc_id)
            state = issues[ref_id]
            if qty > state["remaining"]:
                raise RefError("over_return", doc_id)
            need = qty
            popped = []
            while need:
                receipt, available_here = state["chunks"][-1]
                use = min(need, available_here)
                popped.append([receipt, use])
                need -= use
                if use == available_here:
                    state["chunks"].pop()
                else:
                    state["chunks"][-1] = [receipt, available_here - use]
            popped.reverse()
            state["remaining"] -= qty
            stock[key].extend(popped)
            rows.append(
                {
                    "doc_id": doc_id,
                    "business_time": doc["business_time"],
                    "entry_time": doc["entry_time"],
                    "type": "return",
                    "warehouse_id": doc["warehouse_id"],
                    "material_id": doc["material_id"],
                    "quantity": -qty,
                    "issue_doc_id": ref_id,
                    "sources": _sources(popped, -1),
                }
            )
    return {"status": "ok", "details": rows}
