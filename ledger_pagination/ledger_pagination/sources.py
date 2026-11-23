"""Reconstruct source batches for an issue/return ledger (``trace-sources``).

Given a list of warehouse document versions and two cut-offs (business time and
entry time, both inclusive) the command

1. picks, per stable document id, the latest version *visible at the entry
   cut-off* (voided documents and documents whose selected business time lies
   after the business cut-off are dropped);
2. replays receipts, transfers, issues and returns in ``(business_time,
   doc_id)`` order, keeping a FIFO stock queue per ``(warehouse, material)``;
3. reports, for every participating issue and return, how many units came from
   which receipt batch.

Quantities are integers and are never expanded unit by unit.  The FIFO queues
are implicit treaps of contribution chunks with subtree quantity sums, so a
transfer that moves a whole mixed-batch consignment only re-links a treap root
(``O(1)`` when the destination is empty, ``O(log n)`` otherwise) instead of
walking or copying the batch list.  Returns split the tail of one issue's
deduction sequence, again without copying the whole sequence.

Only logical source details are produced — nothing is written to an archive.
"""

from __future__ import annotations

import json

STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_ERROR = "error"

DOC_TYPES = ("receipt", "transfer", "issue", "return")
_CONTENT_FIELDS = (
    "type",
    "business_time",
    "material_id",
    "warehouse_id",
    "quantity",
    "to_warehouse_id",
    "issue_doc_id",
)

__all__ = ["trace_sources", "STATUS_OK", "STATUS_INVALID", "STATUS_ERROR"]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value):
    return isinstance(value, str) and value != ""


def _canon(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def _invalid(errors):
    return {"status": STATUS_INVALID, "errors": errors}


def _field_error(errors, code, message, input_index):
    errors.append({"code": code, "message": message, "input_index": input_index})


def _doc_error(errors, code, message, rec):
    errors.append(
        {
            "code": code,
            "message": message,
            "input_index": rec["input_index"],
            "doc_id": rec["doc_id"],
            "entry_time": rec["entry_time"],
        }
    )


# ---------------------------------------------------------------------------
# implicit treap queue: FIFO batches with O(log n) split/merge, no bulk copy
# ---------------------------------------------------------------------------

_MASK64 = (1 << 64) - 1


class _Rng:
    """Deterministic splitmix64 — reproducible priorities, no global state."""

    __slots__ = ("state",)

    def __init__(self, seed=0x2545F4914F6CDD1D):
        self.state = seed & _MASK64

    def next(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
        return z ^ (z >> 31)


class _Lot:
    """One contiguous contribution of one receipt batch."""

    __slots__ = ("receipt", "qty", "prio", "left", "right", "total", "size")

    def __init__(self, receipt, qty, prio):
        self.receipt = receipt
        self.qty = qty
        self.prio = prio
        self.left = None
        self.right = None
        self.total = qty
        self.size = 1


class _Queue:
    """Treap of ``_Lot`` nodes ordered by arrival, supporting weighted splits."""

    __slots__ = ("rng", "stats")

    def __init__(self, stats):
        self.rng = _Rng()
        self.stats = stats

    # -- primitives ---------------------------------------------------------
    def _node(self, receipt, qty):
        self.stats["nodes_created"] += 1
        return _Lot(receipt, qty, self.rng.next())

    @staticmethod
    def _pull(node):
        total = node.qty
        size = 1
        if node.left is not None:
            total += node.left.total
            size += node.left.size
        if node.right is not None:
            total += node.right.total
            size += node.right.size
        node.total = total
        node.size = size

    def merge(self, a, b):
        """Concatenate two arrival-ordered treaps."""
        if a is None:
            return b
        if b is None:
            return a
        self.stats["merge_calls"] += 1
        if a.prio > b.prio:
            a.right = self.merge(a.right, b)
            self._pull(a)
            return a
        b.left = self.merge(a, b.left)
        self._pull(b)
        return b

    def split(self, root, k):
        """Return ``(a, b)`` with ``total(a) == k``; ``0 < k < total(root)``."""
        self.stats["split_calls"] += 1
        if root is None:  # pragma: no cover - guarded by callers
            raise AssertionError("split past the end of an empty queue")
        if k <= 0:
            return None, root
        if k >= root.total:
            return root, None
        ltot = root.left.total if root.left is not None else 0
        if k < ltot:
            a, b = self.split(root.left, k)
            root.left = b
            self._pull(root)
            return a, root
        if k > ltot + root.qty:
            a, b = self.split(root.right, k - ltot - root.qty)
            root.right = a
            self._pull(root)
            return root, b
        take = k - ltot
        if take == 0:  # boundary right before this lot
            a = root.left
            root.left = None
            self._pull(root)
            return a, root
        if take == root.qty:  # boundary right after this lot
            b = root.right
            root.right = None
            self._pull(root)
            return root, b
        # boundary cuts through one lot: one receipt, two contributions
        left_part = self._node(root.receipt, take)
        right_part = self._node(root.receipt, root.qty - take)
        return self.merge(root.left, left_part), self.merge(right_part, root.right)

    # -- bulk helpers -------------------------------------------------------
    def build(self, chunks):
        """Build a treap from ordered ``(receipt, qty)`` chunks in O(k)."""
        if not chunks:
            return None
        stack = []
        for receipt, qty in chunks:
            node = self._node(receipt, qty)
            last = None
            while stack and stack[-1].prio < node.prio:
                last = stack.pop()
            node.left = last
            if stack:
                stack[-1].right = node
            stack.append(node)
        root = stack[0]
        self._refresh(root)
        return root

    def _refresh(self, root):
        stack = [(root, False)]
        while stack:
            node, done = stack.pop()
            if done:
                self._pull(node)
            else:
                stack.append((node, True))
                if node.right is not None:
                    stack.append((node.right, False))
                if node.left is not None:
                    stack.append((node.left, False))

    def flatten(self, root):
        """In-order ``[receipt, qty]`` chunks; does not copy the treap."""
        out = []
        stack = []
        cur = root
        while stack or cur is not None:
            while cur is not None:
                stack.append(cur)
                cur = cur.left
            cur = stack.pop()
            out.append([cur.receipt, cur.qty])
            self.stats["leaf_visits"] += 1
            cur = cur.right
        return out


# ---------------------------------------------------------------------------
# version selection
# ---------------------------------------------------------------------------


def _void_flag(raw):
    """Return ``(is_void, ok)`` for the accepted void markers."""
    for flag in ("void", "cancelled", "canceled", "revoked"):
        if flag in raw:
            value = raw[flag]
            if not isinstance(value, bool):
                return False, False
            if value:
                return True, True
    if raw.get("type") == "void":
        return True, True
    return False, True


def _signature(raw, is_void):
    if is_void:
        return ("void",)
    return ("active",) + tuple(
        ("absent",) if field not in raw else _canon(raw[field])
        for field in _CONTENT_FIELDS
    )


def _parse_version(raw, index, entry_cutoff, errors):
    """Return a version record, or ``None`` if it cannot participate.

    A version recorded after the entry cut-off was not visible yet and is
    skipped before any content checks.  Structurally broken visible versions
    are reported.
    """
    where = "versions[%d]" % index
    if not isinstance(raw, dict):
        _field_error(errors, "invalid_field", "%s: must be an object" % where, index)
        return None

    entry_time = raw.get("entry_time")
    if not _is_int(entry_time):
        _field_error(
            errors,
            "invalid_field",
            "%s.entry_time: must be an integer" % where,
            index,
        )
        return None
    if entry_time > entry_cutoff:
        return None  # not visible at the entry cut-off

    doc_id = raw.get("doc_id")
    if not _nonempty_str(doc_id):
        _field_error(
            errors,
            "invalid_field",
            "%s.doc_id: must be a non-empty string" % where,
            index,
        )
        return None

    is_void, flag_ok = _void_flag(raw)
    if not flag_ok:
        _field_error(
            errors,
            "invalid_field",
            "%s: void markers must be booleans" % where,
            index,
        )
        return None

    return {
        "input_index": index,
        "doc_id": doc_id,
        "entry_time": entry_time,
        "void": is_void,
        "raw": raw,
        "sig": _signature(raw, is_void),
    }


def _validate_active(rec, errors):
    """Validate the business content of a selected non-void version."""
    raw = rec["raw"]
    index = rec["input_index"]
    where = "versions[%d]" % index
    doc_id = rec["doc_id"]

    def bad(field, message, code="invalid_field"):
        _doc_error(errors, code, "%s.%s: %s" % (where, field, message), rec)

    dtype = raw.get("type")
    if dtype not in DOC_TYPES:
        bad("type", "must be one of %s" % (", ".join(DOC_TYPES),))
        dtype = None

    business_time = raw.get("business_time")
    if not _is_int(business_time):
        bad("business_time", "must be an integer")

    for field in ("material_id", "warehouse_id"):
        if not _nonempty_str(raw.get(field)):
            bad(field, "must be a non-empty string")

    quantity = raw.get("quantity")
    if not _is_int(quantity) or quantity < 1:
        bad("quantity", "must be a positive integer")

    to_warehouse = raw.get("to_warehouse_id")
    issue_doc = raw.get("issue_doc_id")
    if dtype == "transfer":
        if not _nonempty_str(to_warehouse):
            bad("to_warehouse_id", "must be a non-empty string for a transfer")
        elif to_warehouse == raw.get("warehouse_id"):
            bad(
                "to_warehouse_id",
                "must differ from warehouse_id for a transfer",
                "same_warehouse_transfer",
            )
    if dtype == "return":
        if not _nonempty_str(issue_doc):
            bad("issue_doc_id", "must be a non-empty string for a return")

    return {
        "input_index": index,
        "doc_id": doc_id,
        "entry_time": rec["entry_time"],
        "type": dtype,
        "business_time": business_time,
        "material_id": raw.get("material_id"),
        "warehouse_id": raw.get("warehouse_id"),
        "quantity": quantity,
        "to_warehouse_id": to_warehouse,
        "issue_doc_id": issue_doc,
    }


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


class _TraceFailure(Exception):
    def __init__(self, error):
        super().__init__(error["message"])
        self.error = error


def _fail(code, message, doc, **extra):
    error = {
        "code": code,
        "message": message,
        "doc_id": doc["doc_id"],
        "business_time": doc["business_time"],
        "entry_time": doc["entry_time"],
        "input_index": doc["input_index"],
    }
    error.update(extra)
    raise _TraceFailure(error)


def _aggregate(chunks, sign):
    totals = {}
    for receipt, qty in chunks:
        totals[receipt] = totals.get(receipt, 0) + qty
    return [
        {"receipt_doc_id": receipt, "quantity": sign * totals[receipt]}
        for receipt in sorted(totals)
    ]


class _IssueState:
    __slots__ = ("doc", "chunks", "remaining")

    def __init__(self, doc, chunks):
        self.doc = doc
        self.chunks = chunks  # mutable [receipt, qty], in deduction order
        self.remaining = doc["quantity"]


def _replay(docs):
    """Replay participating docs in order; return result rows or raise."""
    stats = {
        "documents": len(docs),
        "receipts": 0,
        "transfers": 0,
        "issues": 0,
        "returns": 0,
        "nodes_created": 0,
        "split_calls": 0,
        "merge_calls": 0,
        "leaf_visits": 0,
    }
    queue = _Queue(stats)
    stock = {}  # (warehouse, material) -> treap root
    issues = {}  # doc_id -> _IssueState
    receipt_materials = {d["material_id"] for d in docs if d["type"] == "receipt"}
    participating = {d["doc_id"]: d for d in docs}
    order = {d["doc_id"]: i for i, d in enumerate(docs)}
    rows = []

    for index, doc in enumerate(docs):
        dtype = doc["type"]
        material = doc["material_id"]
        warehouse = doc["warehouse_id"]
        qty = doc["quantity"]

        if dtype == "receipt":
            stats["receipts"] += 1
            key = (warehouse, material)
            stock[key] = queue.merge(stock.get(key), queue._node(doc["doc_id"], qty))
            continue

        if dtype == "transfer":
            stats["transfers"] += 1
            source_key = (warehouse, material)
            root = stock.get(source_key)
            available = root.total if root is not None else 0
            if available < qty:
                _fail(
                    "insufficient_stock",
                    "transfer %r needs %d units of material %r at warehouse %r "
                    "but only %d are available"
                    % (doc["doc_id"], qty, material, warehouse, available),
                    doc,
                    document_type="transfer",
                    warehouse_id=warehouse,
                    material_id=material,
                    requested=qty,
                    available=available,
                )
            if qty == available:
                taken, rest = root, None
            else:
                taken, rest = queue.split(root, qty)
            stock[source_key] = rest
            dest_key = (doc["to_warehouse_id"], material)
            stock[dest_key] = queue.merge(stock.get(dest_key), taken)
            continue

        if material not in receipt_materials:
            _fail(
                "no_receipt_evidence",
                "material %r has no participating receipt document in this input"
                % (material,),
                doc,
                document_type=dtype,
                material_id=material,
            )

        if dtype == "issue":
            stats["issues"] += 1
            key = (warehouse, material)
            root = stock.get(key)
            available = root.total if root is not None else 0
            if available < qty:
                _fail(
                    "insufficient_stock",
                    "issue %r needs %d units of material %r at warehouse %r "
                    "but only %d are available"
                    % (doc["doc_id"], qty, material, warehouse, available),
                    doc,
                    document_type="issue",
                    warehouse_id=warehouse,
                    material_id=material,
                    requested=qty,
                    available=available,
                )
            if qty == available:
                taken, rest = root, None
            else:
                taken, rest = queue.split(root, qty)
            stock[key] = rest
            chunks = queue.flatten(taken)
            issues[doc["doc_id"]] = _IssueState(doc, chunks)
            rows.append(
                {
                    "doc_id": doc["doc_id"],
                    "business_time": doc["business_time"],
                    "entry_time": doc["entry_time"],
                    "type": "issue",
                    "warehouse_id": warehouse,
                    "material_id": material,
                    "quantity": qty,
                    "sources": _aggregate(chunks, 1),
                }
            )
            continue

        # return
        stats["returns"] += 1
        ref_id = doc["issue_doc_id"]
        ref_doc = participating.get(ref_id)
        if ref_doc is None:
            reason = "unknown_or_not_participating"
        elif ref_doc["type"] != "issue":
            reason = "not_an_issue_document"
        elif order[ref_id] >= index:
            reason = "not_before_this_return"
        elif ref_doc["warehouse_id"] != warehouse:
            reason = "warehouse_mismatch"
        elif ref_doc["material_id"] != material:
            reason = "material_mismatch"
        else:
            reason = None
        if reason is not None:
            _fail(
                "invalid_return_reference",
                "return %r references issue %r which is not a still-valid, earlier "
                "issue of the same warehouse and material (%s)"
                % (doc["doc_id"], ref_id, reason),
                doc,
                referenced_doc_id=ref_id,
                reason=reason,
            )

        state = issues[ref_id]
        if qty > state.remaining:
            _fail(
                "over_return",
                "return %r asks for %d units from issue %r but only %d remain unreturned"
                % (doc["doc_id"], qty, ref_id, state.remaining),
                doc,
                referenced_doc_id=ref_id,
                requested=qty,
                remaining=state.remaining,
            )

        need = qty
        popped = []
        while need:
            chunk = state.chunks[-1]
            if chunk[1] > need:
                chunk[1] -= need
                popped.append([chunk[0], need])
                need = 0
            else:
                popped.append([chunk[0], chunk[1]])
                need -= chunk[1]
                state.chunks.pop()
        popped.reverse()  # back into original deduction order
        state.remaining -= qty

        key = (warehouse, material)
        stock[key] = queue.merge(stock.get(key), queue.build(popped))
        rows.append(
            {
                "doc_id": doc["doc_id"],
                "business_time": doc["business_time"],
                "entry_time": doc["entry_time"],
                "type": "return",
                "warehouse_id": warehouse,
                "material_id": material,
                "quantity": -qty,
                "issue_doc_id": ref_id,
                "sources": _aggregate(popped, -1),
            }
        )

    return rows, stats


# ---------------------------------------------------------------------------
# public entry
# ---------------------------------------------------------------------------


def trace_sources(payload):
    """Trace issue/return source batches from warehouse documents.

    Input keys: ``business_cutoff`` (int, inclusive), ``entry_cutoff`` (int,
    inclusive) and ``versions`` (list, see :mod:`ledger_pagination.sources`).

    Returns ``{"status": "ok", "details": [...], "stats": {...}}`` on success,
    ``{"status": "invalid", "errors": [...]}`` for malformed input, and
    ``{"status": "error", "error": {...}}`` for a replay failure.  No partial
    details are emitted on failure.
    """
    if not isinstance(payload, dict):
        return _invalid(
            [
                {
                    "code": "invalid_field",
                    "message": "payload must be an object",
                    "input_index": None,
                }
            ]
        )

    errors = []
    business_cutoff = payload.get("business_cutoff")
    entry_cutoff = payload.get("entry_cutoff")
    if not _is_int(business_cutoff):
        _field_error(
            errors,
            "invalid_field",
            "business_cutoff: must be an integer",
            None,
        )
    if not _is_int(entry_cutoff):
        _field_error(errors, "invalid_field", "entry_cutoff: must be an integer", None)

    raw_versions = payload.get("versions")
    if not isinstance(raw_versions, list):
        _field_error(
            errors, "invalid_field", "versions: must be a list of document versions", None
        )
        raw_versions = []

    if errors:
        return _invalid(errors)

    records = []
    for index, raw in enumerate(raw_versions):
        rec = _parse_version(raw, index, entry_cutoff, errors)
        if rec is not None:
            records.append(rec)
    if errors:
        return _invalid(errors)

    # group by (doc_id, entry_time); reject differing content at the same key
    by_doc = {}
    for rec in records:
        by_doc.setdefault(rec["doc_id"], {}).setdefault(rec["entry_time"], []).append(rec)
    for doc_id in sorted(by_doc):
        for entry_time in sorted(by_doc[doc_id]):
            group = by_doc[doc_id][entry_time]
            if len({rec["sig"] for rec in group}) > 1:
                errors.append(
                    {
                        "code": "version_conflict",
                        "message": (
                            "document %r has conflicting versions recorded at entry "
                            "time %d" % (doc_id, entry_time)
                        ),
                        "doc_id": doc_id,
                        "entry_time": entry_time,
                        "input_indices": sorted(rec["input_index"] for rec in group),
                    }
                )
    if errors:
        return _invalid(errors)

    # latest visible version per document wins
    selected_docs = []
    for doc_id in sorted(by_doc):
        entry_time = max(by_doc[doc_id])
        rec = sorted(by_doc[doc_id][entry_time], key=lambda r: r["input_index"])[0]
        if rec["void"]:
            continue
        doc = _validate_active(rec, errors)
        selected_docs.append(doc)
    if errors:
        return _invalid(errors)

    participating = [
        doc for doc in selected_docs if doc["business_time"] <= business_cutoff
    ]
    participating.sort(key=lambda d: (d["business_time"], d["doc_id"]))

    try:
        rows, stats = _replay(participating)
    except _TraceFailure as failure:
        return {"status": STATUS_ERROR, "error": failure.error}

    return {
        "status": STATUS_OK,
        "business_cutoff": business_cutoff,
        "entry_cutoff": entry_cutoff,
        "details": rows,
        "stats": stats,
    }
