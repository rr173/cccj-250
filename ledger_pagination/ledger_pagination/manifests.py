"""Build per-handoff packing manifests from shipping-dock scan events
(``handoff-manifests``).

The dock scans packing, unsealing and handoff operations; the receiving side
needs a per-piece checklist of every consignment that actually left the site.

Input
-----
``objects``
    Catalog of every object the scans may mention.  Each entry has a unique
    non-empty ``id`` and a ``kind`` (``"container"`` or ``"item"``); items also
    carry a non-empty ``material_id``.  Every item id is one physical piece.
    Containers may hold items or other containers, items hold nothing.  All
    objects start on-site, standalone (no parent container) and unsealed.
``events``
    Scan records.  Each has a unique non-negative integer ``seq`` and a
    ``type``; records replay in ascending ``seq`` order — the array position of
    a record carries no meaning.

Events
------
``move``     ``changes``: a non-empty list of ``{"id", "parent_id"}`` details
             (``parent_id: null`` places the object standalone).  One detail
             per object per record, and the parent must actually change.  The
             whole list is one simultaneous handling: only the loading
             relationships before the list as a whole and after it are
             observed, and the final relationships must be acyclic.  Goods
             cannot be taken out of a container whose chain (itself or any
             ancestor) is sealed *before* the move, nor placed into a
             container whose chain is sealed *after* the move.  A sealed box
             itself travels as a whole without disturbing its contents.
``seal``     ``id`` + ``seal_id``: the container must be unsealed and not
             inside another sealed container.
``open``     ``id`` + ``seal_id``: must match the container's active seal;
             the container must not be inside another sealed container.
``handoff``  ``id``: only a standalone container with an active seal may be
             handed off.  The container and everything it then contains leave
             the site; later records may not touch them, not even as a move
             destination.

Output
------
``manifests`` — one entry per handoff, ordered by ascending handoff ``seq``:
``seq``, the handoff container ``id`` and ``objects`` listing the container
with all descendants it held at that moment.  Each object row gives ``id``,
``kind``, the then-current ``parent_id`` (``null`` for the handoff root) and
``material_id`` (items) or the then-current ``seal_id`` (containers, ``null``
when unsealed).  Rows sort by ``id`` in Unicode code point order.  With no
handoffs the list is empty.

Failures (exit code 1, no partial manifests): ``status: "invalid"`` +
``errors`` for malformed catalogs or records (``input_index`` locates the
record, ``seq`` is added when it could be read), ``status: "error"`` +
``error`` for a well-formed record that breaks the handling rules (always
carries the offending record's ``seq``).

Algorithm
---------
The loading forest is a link-cut tree without evert (links always attach a
represented-tree root, so no lazy propagation is needed).  It maintains, per
preferred path, the number of sealed containers, so "is any ancestor sealed"
is one ``access`` away instead of a walk up the whole chain, and a whole-box
re-parenting is one cut plus one link — the contents are never unfolded.  A
``move`` record first validates every detail against the before-state, then
cuts all changed edges, links the new ones (a link whose destination is
already in the moved subtree would close a cycle), checks the destination
chains against the after-state, and rolls the tentative links/cuts back if
any check fails.  Each re-parenting detail and each seal/open costs
``O(log n)`` amortized — within the required ``O(log^2 n)`` — independent of
nesting depth; only the objects actually printed into a handoff manifest are
enumerated (once, when they leave).  Sorting the input by ``seq`` is not
counted against this.
"""

from __future__ import annotations

STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_ERROR = "error"

__all__ = ["handoff_manifests", "STATUS_OK", "STATUS_INVALID", "STATUS_ERROR"]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value):
    return isinstance(value, str) and value != ""


def _invalid(errors):
    return {"status": STATUS_INVALID, "errors": errors}


def _field_error(errors, code, message, input_index, seq=None):
    error = {"code": code, "message": message, "input_index": input_index}
    if seq is not None:
        error["seq"] = seq
    errors.append(error)


# ---------------------------------------------------------------------------
# link-cut tree without evert: sealed-count aggregates on root-to-node paths
# ---------------------------------------------------------------------------


class _Forest:
    """Dynamic forest over node ids ``1..n`` (``0`` is the null sentinel).

    Only represented-tree roots are ever linked, so no make-root/lazy reversal
    is needed.  ``agg`` sums the ``sealed`` flags over each aux splay, hence
    after :meth:`access` the aggregate of a node is the number of sealed
    containers on the represented path from the tree root down to it.
    """

    __slots__ = ("fa", "ch0", "ch1", "sealed", "agg", "stats")

    def __init__(self, n, stats):
        self.fa = [0] * (n + 1)
        self.ch0 = [0] * (n + 1)
        self.ch1 = [0] * (n + 1)
        self.sealed = [0] * (n + 1)
        self.agg = [0] * (n + 1)
        self.stats = stats

    def _is_splay_root(self, x):
        f = self.fa[x]
        return f == 0 or (self.ch0[f] != x and self.ch1[f] != x)

    def _pull(self, x):
        self.agg[x] = self.sealed[x] + self.agg[self.ch0[x]] + self.agg[self.ch1[x]]

    def _rotate(self, x):
        self.stats["splay_rotations"] += 1
        f = self.fa[x]
        g = self.fa[f]
        if self.ch0[f] == x:
            b = self.ch1[x]
            self.ch1[x] = f
            self.ch0[f] = b
            if b:
                self.fa[b] = f
        else:
            b = self.ch0[x]
            self.ch0[x] = f
            self.ch1[f] = b
            if b:
                self.fa[b] = f
        self.fa[f] = x
        self.fa[x] = g
        if g:
            if self.ch0[g] == f:
                self.ch0[g] = x
            elif self.ch1[g] == f:
                self.ch1[g] = x
        self._pull(f)
        self._pull(x)

    def _splay(self, x):
        self.stats["splays"] += 1
        while not self._is_splay_root(x):
            f = self.fa[x]
            g = self.fa[f]
            if self._is_splay_root(f):
                self._rotate(x)
            elif (self.ch0[f] == x) == (self.ch0[g] == f):
                self._rotate(f)
                self._rotate(x)
            else:
                self._rotate(x)
                self._rotate(x)

    def access(self, x):
        """Expose the represented path root->x as x's splay; splay x."""
        self.stats["accesses"] += 1
        last = 0
        u = x
        while u:
            self._splay(u)
            self.ch1[u] = last
            if last:
                self.fa[last] = u
            self._pull(u)
            last = u
            u = self.fa[u]
        self._splay(x)

    def find_root(self, x):
        """Represented-tree root of x's tree."""
        self.stats["root_queries"] += 1
        self.access(x)
        y = x
        while self.ch0[y]:
            y = self.ch0[y]
        self._splay(y)
        return y

    def link_root_under(self, x, y):
        """Make y the parent of x; x must currently be a represented root."""
        self.stats["links"] += 1
        self.access(x)
        self.fa[x] = y

    def cut_parent_edge(self, x):
        """Detach x from its represented parent; x must have one."""
        self.stats["cuts"] += 1
        self.access(x)
        left = self.ch0[x]
        self.ch0[x] = 0
        if left:
            self.fa[left] = 0
        self._pull(x)

    def sealed_on_path(self, x):
        """Number of sealed containers on the represented path root->x."""
        self.access(x)
        return self.agg[x]

    def topmost_sealed(self, x):
        """The sealed node nearest the root on path root->x (0 if none)."""
        self.access(x)
        if not self.agg[x]:
            return 0
        node = x
        while True:
            left = self.ch0[node]
            if left and self.agg[left]:
                node = left
            elif self.sealed[node]:
                return node
            else:
                node = self.ch1[node]

    def set_sealed(self, x, value):
        self.access(x)
        self.sealed[x] = 1 if value else 0
        self._pull(x)


# ---------------------------------------------------------------------------
# structural validation
# ---------------------------------------------------------------------------


def _parse_objects(raw_objects, errors):
    positions = {}
    parsed = []
    for index, raw in enumerate(raw_objects):
        where = "objects[%d]" % index
        if not isinstance(raw, dict):
            _field_error(errors, "invalid_field", "%s: must be an object" % where, index)
            continue
        oid = raw.get("id")
        if not _nonempty_str(oid):
            _field_error(
                errors, "invalid_field", "%s.id: must be a non-empty string" % where, index
            )
            continue
        kind = raw.get("kind")
        if kind not in ("container", "item"):
            _field_error(
                errors,
                "invalid_field",
                '%s.kind: must be "container" or "item"' % where,
                index,
            )
            continue
        material = raw.get("material_id")
        if kind == "item" and not _nonempty_str(material):
            _field_error(
                errors,
                "invalid_field",
                "%s.material_id: an item must name a non-empty material id" % where,
                index,
            )
            continue
        parsed.append((oid, kind, material if kind == "item" else None))
        positions.setdefault(oid, []).append(index)
    for oid in sorted(positions):
        pos = positions[oid]
        if len(pos) > 1:
            errors.append(
                {
                    "code": "duplicate_object",
                    "message": "object id %r is declared %d times" % (oid, len(pos)),
                    "object_id": oid,
                    "input_indices": pos,
                }
            )
    if errors:
        return None
    return {
        "ids": [entry[0] for entry in parsed],
        "kinds": [entry[1] for entry in parsed],
        "materials": [entry[2] for entry in parsed],
    }


def _parse_events(raw_events, errors):
    events = []
    seq_positions = {}
    for index, raw in enumerate(raw_events):
        where = "events[%d]" % index
        if not isinstance(raw, dict):
            _field_error(errors, "invalid_field", "%s: must be an object" % where, index)
            continue
        seq = raw.get("seq")
        if not _is_int(seq) or seq < 0:
            _field_error(
                errors,
                "invalid_field",
                "%s.seq: must be a non-negative integer" % where,
                index,
            )
            continue
        seq_positions.setdefault(seq, []).append(index)
        etype = raw.get("type")
        if etype == "move":
            changes = raw.get("changes")
            if not isinstance(changes, list) or not changes:
                _field_error(
                    errors,
                    "invalid_field",
                    "%s.changes: must be a non-empty list of re-parenting details"
                    % where,
                    index,
                    seq,
                )
                continue
            parsed_changes = []
            ok = True
            for j, change in enumerate(changes):
                cwhere = "%s.changes[%d]" % (where, j)
                if not isinstance(change, dict):
                    _field_error(
                        errors, "invalid_field", "%s: must be an object" % cwhere, index, seq
                    )
                    ok = False
                    continue
                cid = change.get("id")
                if not _nonempty_str(cid):
                    _field_error(
                        errors,
                        "invalid_field",
                        "%s.id: must be a non-empty string" % cwhere,
                        index,
                        seq,
                    )
                    ok = False
                    continue
                if "parent_id" not in change:
                    _field_error(
                        errors,
                        "invalid_field",
                        "%s.parent_id: key is required (null places the object "
                        "standalone)" % cwhere,
                        index,
                        seq,
                    )
                    ok = False
                    continue
                pid = change["parent_id"]
                if pid is not None and not _nonempty_str(pid):
                    _field_error(
                        errors,
                        "invalid_field",
                        "%s.parent_id: must be null or a non-empty string" % cwhere,
                        index,
                        seq,
                    )
                    ok = False
                    continue
                parsed_changes.append({"id": cid, "parent_id": pid})
            if not ok:
                continue
            events.append(
                {
                    "seq": seq,
                    "input_index": index,
                    "type": "move",
                    "changes": parsed_changes,
                }
            )
            continue
        if etype in ("seal", "open"):
            cid = raw.get("id")
            sid = raw.get("seal_id")
            ok = True
            if not _nonempty_str(cid):
                _field_error(
                    errors,
                    "invalid_field",
                    "%s.id: must be a non-empty string" % where,
                    index,
                    seq,
                )
                ok = False
            if not _nonempty_str(sid):
                _field_error(
                    errors,
                    "invalid_field",
                    "%s.seal_id: must be a non-empty string" % where,
                    index,
                    seq,
                )
                ok = False
            if not ok:
                continue
            events.append(
                {
                    "seq": seq,
                    "input_index": index,
                    "type": etype,
                    "id": cid,
                    "seal_id": sid,
                }
            )
            continue
        if etype == "handoff":
            cid = raw.get("id")
            if not _nonempty_str(cid):
                _field_error(
                    errors,
                    "invalid_field",
                    "%s.id: must be a non-empty string" % where,
                    index,
                    seq,
                )
                continue
            events.append(
                {"seq": seq, "input_index": index, "type": "handoff", "id": cid}
            )
            continue
        _field_error(
            errors,
            "invalid_field",
            '%s.type: must be one of "move", "seal", "open", "handoff"' % where,
            index,
            seq,
        )
    for seq in sorted(seq_positions):
        pos = seq_positions[seq]
        if len(pos) > 1:
            errors.append(
                {
                    "code": "duplicate_seq",
                    "message": "seq %d is shared by %d events" % (seq, len(pos)),
                    "seq": seq,
                    "input_indices": pos,
                }
            )
    events.sort(key=lambda event: event["seq"])
    return events


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


class _ScanFailure(Exception):
    def __init__(self, error):
        super().__init__(error["message"])
        self.error = error


def _fail(code, message, event, **extra):
    error = {
        "code": code,
        "message": message,
        "seq": event["seq"],
        "input_index": event["input_index"],
    }
    error.update(extra)
    raise _ScanFailure(error)


def _replay(objects, events, stats):
    ids = objects["ids"]
    kinds = objects["kinds"]
    materials = objects["materials"]
    n = len(ids)
    idmap = {oid: i for i, oid in enumerate(ids)}
    parent = [-1] * n  # parent object index, -1 = standalone
    children = [None] * n  # lazily created sets of child indices
    seal = [None] * n  # active seal id per container, None = unsealed
    departed = [False] * n
    forest = _Forest(n, stats)
    manifests = []

    def add_child(c, x):
        bucket = children[c]
        if bucket is None:
            bucket = children[c] = set()
        bucket.add(x)

    def lookup(oid, event, role):
        x = idmap.get(oid)
        if x is None:
            _fail(
                "unknown_object",
                "%s %r is not in the object catalog" % (role, oid),
                event,
                object_id=oid,
            )
        if departed[x]:
            _fail(
                "departed_object",
                "%s %r already left the site in an earlier handoff" % (role, oid),
                event,
                object_id=oid,
            )
        return x

    def rollback(recs, linked):
        # undo the tentative state: drop every new edge, restore every old one
        for x in linked:
            forest.cut_parent_edge(x + 1)
        for x, old, _new, _oid, _pid in recs:
            if old != -1:
                forest.link_root_under(x + 1, old + 1)

    def do_move(event):
        stats["moves"] += 1
        changes = event["changes"]
        stats["change_details"] += len(changes)
        seen = set()
        recs = []
        for change in changes:
            oid = change["id"]
            if oid not in idmap:
                _fail(
                    "unknown_object",
                    "moved object %r is not in the object catalog" % oid,
                    event,
                    object_id=oid,
                )
            x = idmap[oid]
            if x in seen:
                _fail(
                    "duplicate_change",
                    "object %r is re-parented more than once in this move" % oid,
                    event,
                    object_id=oid,
                )
            seen.add(x)
            if departed[x]:
                _fail(
                    "departed_object",
                    "object %r already left the site in an earlier handoff" % oid,
                    event,
                    object_id=oid,
                )
            pid = change["parent_id"]
            if pid is None:
                y = -1
            else:
                if pid not in idmap:
                    _fail(
                        "unknown_object",
                        "move destination %r is not in the object catalog" % pid,
                        event,
                        object_id=pid,
                    )
                y = idmap[pid]
                if departed[y]:
                    _fail(
                        "departed_object",
                        "move destination %r already left the site in an earlier "
                        "handoff" % pid,
                        event,
                        object_id=pid,
                    )
                if kinds[y] != "container":
                    _fail(
                        "item_as_parent",
                        "move destination %r is an item and cannot hold anything"
                        % pid,
                        event,
                        object_id=pid,
                    )
            if y == parent[x]:
                _fail(
                    "no_change",
                    "object %r is already %s"
                    % (oid, "standalone" if y == -1 else "inside %r" % pid),
                    event,
                    object_id=oid,
                    parent_id=pid,
                )
            recs.append((x, parent[x], y, oid, pid))

        # before-state: nothing may be taken out of a sealed chain
        for x, old, _new, oid, _pid in recs:
            if old != -1 and forest.sealed_on_path(old + 1):
                blocker = forest.topmost_sealed(old + 1)
                _fail(
                    "sealed_source",
                    "cannot take %r out of %r: container %r is sealed"
                    % (oid, ids[old], ids[blocker - 1]),
                    event,
                    object_id=oid,
                    container_id=ids[old],
                    sealed_container_id=ids[blocker - 1],
                )

        # tentative apply: cut every changed edge, then link every new parent
        linked = []
        cycling = None
        for x, old, _new, _oid, _pid in recs:
            if old != -1:
                forest.cut_parent_edge(x + 1)
        for x, _old, new, oid, pid in recs:
            if new != -1:
                if forest.find_root(new + 1) == x + 1:
                    cycling = (oid, pid)
                    break
                forest.link_root_under(x + 1, new + 1)
                linked.append(x)
        if cycling is not None:
            rollback(recs, linked)
            _fail(
                "cycle",
                "re-parenting %r under %r would close a loading cycle" % cycling,
                event,
                object_id=cycling[0],
                parent_id=cycling[1],
            )

        # after-state: nothing may be placed into a sealed chain
        for x, _old, new, oid, pid in recs:
            if new != -1 and forest.sealed_on_path(new + 1):
                blocker = forest.topmost_sealed(new + 1)
                rollback(recs, linked)
                _fail(
                    "sealed_destination",
                    "cannot place %r into %r: container %r is sealed"
                    % (oid, pid, ids[blocker - 1]),
                    event,
                    object_id=oid,
                    parent_id=pid,
                    sealed_container_id=ids[blocker - 1],
                )

        # commit
        for x, old, new, _oid, _pid in recs:
            if old != -1:
                children[old].discard(x)
            if new != -1:
                add_child(new, x)
            parent[x] = new

    def do_seal(event):
        stats["seals"] += 1
        cid = event["id"]
        c = lookup(cid, event, "sealed container")
        if kinds[c] != "container":
            _fail(
                "not_a_container",
                "object %r is an item and cannot be sealed" % cid,
                event,
                object_id=cid,
            )
        if seal[c] is not None:
            _fail(
                "already_sealed",
                "container %r already carries seal %r" % (cid, seal[c]),
                event,
                object_id=cid,
                seal_id=seal[c],
            )
        if forest.sealed_on_path(c + 1):
            blocker = forest.topmost_sealed(c + 1)
            _fail(
                "sealed_ancestor",
                "container %r sits inside sealed container %r" % (cid, ids[blocker - 1]),
                event,
                object_id=cid,
                sealed_container_id=ids[blocker - 1],
            )
        forest.set_sealed(c + 1, True)
        seal[c] = event["seal_id"]

    def do_open(event):
        stats["opens"] += 1
        cid = event["id"]
        c = lookup(cid, event, "opened container")
        if kinds[c] != "container":
            _fail(
                "not_a_container",
                "object %r is an item and cannot be opened" % cid,
                event,
                object_id=cid,
            )
        if seal[c] is None:
            _fail(
                "not_sealed",
                "container %r carries no active seal" % cid,
                event,
                object_id=cid,
            )
        if forest.sealed_on_path(c + 1) > 1:
            blocker = forest.topmost_sealed(c + 1)
            _fail(
                "sealed_ancestor",
                "container %r sits inside sealed container %r" % (cid, ids[blocker - 1]),
                event,
                object_id=cid,
                sealed_container_id=ids[blocker - 1],
            )
        if seal[c] != event["seal_id"]:
            _fail(
                "seal_mismatch",
                "container %r carries seal %r, not %r" % (cid, seal[c], event["seal_id"]),
                event,
                object_id=cid,
                seal_id=event["seal_id"],
                current_seal_id=seal[c],
            )
        forest.set_sealed(c + 1, False)
        seal[c] = None

    def do_handoff(event):
        stats["handoffs"] += 1
        cid = event["id"]
        c = lookup(cid, event, "handoff container")
        if kinds[c] != "container":
            _fail(
                "not_a_container",
                "object %r is an item and cannot be handed off as a container" % cid,
                event,
                object_id=cid,
            )
        if parent[c] != -1:
            _fail(
                "not_standalone",
                "container %r is still inside %r" % (cid, ids[parent[c]]),
                event,
                object_id=cid,
                parent_id=ids[parent[c]],
            )
        if seal[c] is None:
            _fail(
                "not_sealed",
                "container %r carries no active seal" % cid,
                event,
                object_id=cid,
            )
        # enumerate the leaving subtree once — these objects are exactly the
        # manifest rows, so this walk is exempt from the per-scan budget
        members = []
        stack = [c]
        while stack:
            u = stack.pop()
            departed[u] = True
            members.append(u)
            bucket = children[u]
            if bucket:
                stack.extend(bucket)
        entries = []
        for u in members:
            entry = {
                "id": ids[u],
                "kind": kinds[u],
                "parent_id": ids[parent[u]] if parent[u] != -1 else None,
            }
            if kinds[u] == "item":
                entry["material_id"] = materials[u]
            else:
                entry["seal_id"] = seal[u]
            entries.append(entry)
        entries.sort(key=lambda row: row["id"])
        stats["manifest_objects"] += len(entries)
        manifests.append({"seq": event["seq"], "id": cid, "objects": entries})

    handlers = {
        "move": do_move,
        "seal": do_seal,
        "open": do_open,
        "handoff": do_handoff,
    }
    for event in events:
        handlers[event["type"]](event)
    return manifests


# ---------------------------------------------------------------------------
# public entry
# ---------------------------------------------------------------------------


def handoff_manifests(payload):
    """Replay shipping-dock scans and build one manifest per handoff.

    Returns ``{"status": "ok", "manifests": [...], "stats": {...}}`` on
    success, ``{"status": "invalid", "errors": [...]}`` for malformed input
    and ``{"status": "error", "error": {...}}`` when a well-formed record
    breaks the handling rules.  No partial manifests are emitted on failure.
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
    raw_objects = payload.get("objects")
    if not isinstance(raw_objects, list):
        _field_error(
            errors, "invalid_field", "objects: must be a list of object entries", None
        )
        raw_objects = []
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        _field_error(
            errors, "invalid_field", "events: must be a list of scan events", None
        )
        raw_events = []
    if errors:
        return _invalid(errors)

    objects = _parse_objects(raw_objects, errors)
    events = _parse_events(raw_events, errors)
    if errors:
        return _invalid(errors)

    stats = {
        "objects": len(objects["ids"]),
        "events": len(events),
        "moves": 0,
        "seals": 0,
        "opens": 0,
        "handoffs": 0,
        "change_details": 0,
        "manifest_objects": 0,
        "accesses": 0,
        "links": 0,
        "cuts": 0,
        "root_queries": 0,
        "splays": 0,
        "splay_rotations": 0,
    }
    try:
        manifests = _replay(objects, events, stats)
    except _ScanFailure as failure:
        return {"status": STATUS_ERROR, "error": failure.error}

    return {"status": STATUS_OK, "manifests": manifests, "stats": stats}
