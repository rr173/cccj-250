"""Naive reference implementation of ``handoff_manifests`` for cross-checking.

Deliberately dumb: ancestor chains are walked link by link, cycle detection
walks the parent map node by node, and handoff enumeration scans the whole
catalog.  Only suitable for small instances; the production implementation in
``ledger_pagination/manifests.py`` must agree with it.

Assumes a structurally valid payload (structural validation is exercised by
dedicated unit tests, not by randomized cross-checking).  Mirrors the
production check order exactly so both agree on the reported error code.
"""

from __future__ import annotations


class RefError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def ref_manifests(payload):
    ids = [obj["id"] for obj in payload["objects"]]
    kind = {obj["id"]: obj["kind"] for obj in payload["objects"]}
    material = {obj["id"]: obj.get("material_id") for obj in payload["objects"]}
    parent = {oid: None for oid in ids}
    seal = {oid: None for oid in ids}
    departed = set()
    manifests = []

    def chain_has_seal(start, pmap):
        node = start
        while node is not None:
            if seal[node] is not None:
                return True
            node = pmap[node]
        return False

    for event in sorted(payload["events"], key=lambda ev: ev["seq"]):
        etype = event["type"]
        if etype == "move":
            recs = []
            seen = set()
            for change in event["changes"]:
                oid = change["id"]
                if oid not in parent:
                    raise RefError("unknown_object")
                if oid in seen:
                    raise RefError("duplicate_change")
                seen.add(oid)
                if oid in departed:
                    raise RefError("departed_object")
                pid = change["parent_id"]
                if pid is not None:
                    if pid not in parent:
                        raise RefError("unknown_object")
                    if pid in departed:
                        raise RefError("departed_object")
                    if kind[pid] != "container":
                        raise RefError("item_as_parent")
                if pid == parent[oid]:
                    raise RefError("no_change")
                recs.append((oid, parent[oid], pid))
            for _oid, old, _new in recs:
                if old is not None and chain_has_seal(old, parent):
                    raise RefError("sealed_source")
            newparent = dict(parent)
            for oid, _old, new in recs:
                newparent[oid] = new
            for oid, _old, _new in recs:
                node = oid
                walked = set()
                while node is not None and node not in walked:
                    walked.add(node)
                    node = newparent[node]
                if node is not None:
                    raise RefError("cycle")
            for _oid, _old, new in recs:
                if new is not None and chain_has_seal(new, newparent):
                    raise RefError("sealed_destination")
            for oid, _old, new in recs:
                parent[oid] = new
            continue

        cid = event["id"]
        if cid not in parent:
            raise RefError("unknown_object")
        if cid in departed:
            raise RefError("departed_object")
        if etype == "handoff":
            if kind[cid] != "container":
                raise RefError("not_a_container")
            if parent[cid] is not None:
                raise RefError("not_standalone")
            if seal[cid] is None:
                raise RefError("not_sealed")
            kids = {oid: [] for oid in ids}
            for oid in ids:
                if parent[oid] is not None:
                    kids[parent[oid]].append(oid)
            members = []
            stack = [cid]
            while stack:
                node = stack.pop()
                departed.add(node)
                members.append(node)
                stack.extend(kids[node])
            entries = []
            for oid in members:
                entry = {"id": oid, "kind": kind[oid], "parent_id": parent[oid]}
                if kind[oid] == "item":
                    entry["material_id"] = material[oid]
                else:
                    entry["seal_id"] = seal[oid]
                entries.append(entry)
            entries.sort(key=lambda row: row["id"])
            manifests.append({"seq": event["seq"], "id": cid, "objects": entries})
            continue

        if kind[cid] != "container":
            raise RefError("not_a_container")
        if etype == "seal":
            if seal[cid] is not None:
                raise RefError("already_sealed")
            if parent[cid] is not None and chain_has_seal(parent[cid], parent):
                raise RefError("sealed_ancestor")
            seal[cid] = event["seal_id"]
        else:  # open
            if seal[cid] is None:
                raise RefError("not_sealed")
            if parent[cid] is not None and chain_has_seal(parent[cid], parent):
                raise RefError("sealed_ancestor")
            if seal[cid] != event["seal_id"]:
                raise RefError("seal_mismatch")
            seal[cid] = None
    return {"status": "ok", "manifests": manifests}
