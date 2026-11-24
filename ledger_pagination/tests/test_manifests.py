"""Tests for the ``handoff-manifests`` command.

Runnable both under pytest and standalone via ``run_tests.py`` (plain asserts).

Covers, as required:
* the worked sample (atomic in-one-record swap of two containers, whole-box
  moves of a sealed consignment, two handoffs) as a directly checkable file;
* move semantics: simultaneous group application (no order-dependent cycle
  misjudgement), sealed source/destination chains before/after the move,
  sealed boxes travelling as a whole, cycle detection, no-change and
  duplicate details, items as destinations, unknown/departed objects;
* seal/open rules: double sealing, seal-id match on opening, neither allowed
  inside another sealed container;
* handoff rules: standalone + active seal only, departed objects untouchable
  afterwards (including as move destinations), manifests ordered by seq,
  objects sorted by id code points, empty manifest list without handoffs;
* structural invalidity reported with input positions (and seq when readable);
* randomized cross-check against the explicit naive reference;
* scale: deep nesting and long runs of whole-box re-parentings cost
  ~O(log n) per detail (splay rotations counted), contents are enumerated
  only when actually listed in a handoff manifest.
"""

import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger_pagination import handoff_manifests
from manifests_ref import RefError, ref_manifests

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def container(oid):
    return {"id": oid, "kind": "container"}


def item(oid, material="M"):
    return {"id": oid, "material_id": material, "kind": "item"}


def move(seq, changes):
    return {
        "seq": seq,
        "type": "move",
        "changes": [{"id": cid, "parent_id": pid} for cid, pid in changes],
    }


def seal(seq, cid, seal_id="S"):
    return {"seq": seq, "type": "seal", "id": cid, "seal_id": seal_id}


def open_(seq, cid, seal_id="S"):
    return {"seq": seq, "type": "open", "id": cid, "seal_id": seal_id}


def handoff(seq, cid):
    return {"seq": seq, "type": "handoff", "id": cid}


def payload(objects, events):
    return {"objects": objects, "events": events}


def ok(res):
    assert res["status"] == "ok", res
    return res


def err(res, code):
    assert res["status"] == "error", res
    assert res["error"]["code"] == code, res
    return res["error"]


def invalid(res):
    assert res["status"] == "invalid", res
    return res["errors"]


# ---------------------------------------------------------------------------
# move semantics
# ---------------------------------------------------------------------------


def test_atomic_swap_in_one_record_is_not_a_cycle():
    # B sits in A; one record lands B and packs A into B (spec example)
    objects = [container("A"), container("B"), item("I1")]
    events = [
        move(1, [("I1", "A"), ("B", "A")]),
        move(2, [("B", None), ("A", "B")]),
        seal(3, "B", "S-1"),
        handoff(4, "B"),
    ]
    res = ok(handoff_manifests(payload(objects, events)))
    (manifest,) = res["manifests"]
    by_id = {row["id"]: row for row in manifest["objects"]}
    assert by_id["A"]["parent_id"] == "B"
    assert by_id["B"]["parent_id"] is None
    assert by_id["I1"]["parent_id"] == "A"


def test_change_order_inside_record_does_not_matter():
    objects = [container("A"), container("B"), item("I1")]
    base = [move(1, [("I1", "A"), ("B", "A")])]
    tail = [seal(3, "B"), handoff(4, "B")]
    forward = payload(objects, base + [move(2, [("B", None), ("A", "B")])] + tail)
    backward = payload(objects, base + [move(2, [("A", "B"), ("B", None)])] + tail)
    res_f = handoff_manifests(forward)
    res_b = handoff_manifests(backward)
    assert res_f["status"] == res_b["status"] == "ok"
    assert res_f["manifests"] == res_b["manifests"]


def test_cycle_and_self_cycle_detected():
    objects = [container("A"), container("B"), container("C")]
    res = handoff_manifests(
        payload(objects, [move(1, [("A", "B"), ("B", "C"), ("C", "A")])])
    )
    err(res, "cycle")
    res = handoff_manifests(payload(objects, [move(1, [("A", "A")])]))
    error = err(res, "cycle")
    assert error["seq"] == 1 and error["input_index"] == 0


def test_move_into_own_descendant_is_a_cycle():
    objects = [container("A"), container("B"), container("C")]
    events = [move(1, [("B", "A"), ("C", "B")]), move(2, [("A", "C")])]
    err(handoff_manifests(payload(objects, events)), "cycle")


def test_sealed_source_chain_blocks_taking_out():
    objects = [container("A"), container("B"), item("I1")]
    events = [
        move(1, [("I1", "A"), ("A", "B")]),
        seal(2, "B"),
        move(3, [("I1", None)]),
    ]
    error = err(handoff_manifests(payload(objects, events)), "sealed_source")
    assert error["seq"] == 3 and error["sealed_container_id"] == "B"


def test_sealed_destination_chain_blocks_putting_in():
    objects = [container("A"), container("B"), item("I1")]
    events = [move(1, [("A", "B")]), seal(2, "B"), move(3, [("I1", "A")])]
    error = err(handoff_manifests(payload(objects, events)), "sealed_destination")
    assert error["sealed_container_id"] == "B"


def test_sealed_box_travels_as_a_whole():
    # a sealed box may be re-parented; its contents are untouched
    objects = [container("A"), container("B"), item("I1")]
    events = [
        move(1, [("I1", "A")]),
        seal(2, "A", "S-9"),
        move(3, [("A", "B")]),
        move(4, [("A", None)]),
        handoff(5, "A"),
    ]
    res = ok(handoff_manifests(payload(objects, events)))
    (manifest,) = res["manifests"]
    by_id = {row["id"]: row for row in manifest["objects"]}
    assert by_id["I1"]["parent_id"] == "A"
    assert by_id["A"]["seal_id"] == "S-9"
    assert by_id["A"]["parent_id"] is None


def test_forest_matches_naive_model():
    # the link-cut tree itself, checked against a plain parent-array model:
    # random whole-subtree re-parentings, cuts, seals and path queries
    from ledger_pagination.manifests import _Forest

    rng = random.Random(20261124)
    n = 300
    stats = {"splay_rotations": 0, "accesses": 0, "links": 0, "cuts": 0,
             "root_queries": 0, "splays": 0}
    forest = _Forest(n, stats)
    parent = [None] * n
    sealed = [False] * n

    def naive_sealed_on_path(x):
        node = x
        total = 0
        while node is not None:
            total += sealed[node]
            node = parent[node]
        return total

    def naive_root(x):
        while parent[x] is not None:
            x = parent[x]
        return x

    for _step in range(6000):
        roll = rng.random()
        if roll < 0.35:
            # cut a random non-root node from its parent
            candidates = [x for x in range(n) if parent[x] is not None]
            if not candidates:
                continue
            x = rng.choice(candidates)
            forest.cut_parent_edge(x + 1)
            parent[x] = None
        elif roll < 0.70:
            # link a random root under a node of another tree
            roots = [x for x in range(n) if parent[x] is None]
            x = rng.choice(roots)
            y = rng.randrange(n)
            if naive_root(y) == x:
                continue
            forest.link_root_under(x + 1, y + 1)
            parent[x] = y
        elif roll < 0.85:
            x = rng.randrange(n)
            value = rng.random() < 0.5
            forest.set_sealed(x + 1, value)
            sealed[x] = value
        else:
            x = rng.randrange(n)
            assert forest.sealed_on_path(x + 1) == naive_sealed_on_path(x)
            assert forest.find_root(x + 1) == naive_root(x) + 1
    for x in range(n):
        assert forest.sealed_on_path(x + 1) == naive_sealed_on_path(x)


def test_no_change_and_duplicate_change_rejected():
    objects = [container("A"), item("I1")]
    err(handoff_manifests(payload(objects, [move(1, [("I1", None)])])), "no_change")
    events = [move(1, [("I1", "A")]), move(2, [("I1", "A")])]
    err(handoff_manifests(payload(objects, events)), "no_change")
    err(
        handoff_manifests(payload(objects, [move(1, [("I1", "A"), ("I1", None)])])),
        "duplicate_change",
    )


def test_item_cannot_be_parent_and_unknown_objects():
    objects = [container("A"), item("I1")]
    err(handoff_manifests(payload(objects, [move(1, [("A", "I1")])])), "item_as_parent")
    err(handoff_manifests(payload(objects, [move(1, [("ZZ", "A")])])), "unknown_object")
    err(handoff_manifests(payload(objects, [move(1, [("A", "ZZ")])])), "unknown_object")


# ---------------------------------------------------------------------------
# seal / open
# ---------------------------------------------------------------------------


def test_seal_open_roundtrip():
    objects = [container("A"), item("I1")]
    events = [
        move(1, [("I1", "A")]),
        seal(2, "A", "S-1"),
        open_(3, "A", "S-1"),
        seal(4, "A", "S-2"),
        handoff(5, "A"),
    ]
    res = ok(handoff_manifests(payload(objects, events)))
    (manifest,) = res["manifests"]
    by_id = {row["id"]: row for row in manifest["objects"]}
    assert by_id["A"]["seal_id"] == "S-2"


def test_double_seal_and_open_unsealed_rejected():
    objects = [container("A")]
    err(handoff_manifests(payload(objects, [seal(1, "A"), seal(2, "A", "T")])), "already_sealed")
    err(handoff_manifests(payload(objects, [open_(1, "A")])), "not_sealed")


def test_open_requires_matching_seal_id():
    objects = [container("A")]
    res = handoff_manifests(payload(objects, [seal(1, "A", "S"), open_(2, "A", "X")]))
    error = err(res, "seal_mismatch")
    assert error["current_seal_id"] == "S" and error["seal_id"] == "X"


def test_seal_and_open_blocked_inside_sealed_container():
    objects = [container("A"), container("B")]
    events = [move(1, [("A", "B")]), seal(2, "B"), seal(3, "A")]
    err(handoff_manifests(payload(objects, events)), "sealed_ancestor")
    events = [move(1, [("A", "B")]), seal(2, "A"), seal(3, "B"), open_(4, "A")]
    err(handoff_manifests(payload(objects, events)), "sealed_ancestor")


def test_seal_open_on_item_rejected():
    objects = [item("I1")]
    err(handoff_manifests(payload(objects, [seal(1, "I1")])), "not_a_container")
    err(handoff_manifests(payload(objects, [open_(1, "I1")])), "not_a_container")


# ---------------------------------------------------------------------------
# handoff
# ---------------------------------------------------------------------------


def test_handoff_requires_standalone_sealed_container():
    objects = [container("A"), container("B"), item("I1")]
    err(handoff_manifests(payload(objects, [handoff(1, "A")])), "not_sealed")
    events = [move(1, [("A", "B")]), seal(2, "A"), handoff(3, "A")]
    err(handoff_manifests(payload(objects, events)), "not_standalone")
    err(handoff_manifests(payload(objects, [handoff(1, "I1")])), "not_a_container")


def test_departed_objects_are_untouchable():
    objects = [container("A"), container("B"), item("I1")]
    prefix = [move(1, [("I1", "A")]), seal(2, "A"), handoff(3, "A")]
    for tail in (
        [move(4, [("I1", "B")])],  # moved object departed
        [move(4, [("B", "A")])],  # destination departed
        [seal(4, "A")],  # seal departed
        [open_(4, "A")],  # open departed
        [handoff(4, "A")],  # hand off twice
    ):
        err(handoff_manifests(payload(objects, prefix + tail)), "departed_object")


def test_manifests_ordered_by_seq_and_rows_by_codepoint():
    objects = [container("b"), container("A"), item("10"), item("2")]
    events = [
        move(10, [("10", "b"), ("2", "b")]),
        seal(20, "b"),
        seal(5, "A"),
        handoff(30, "b"),
        handoff(15, "A"),
    ]
    res = ok(handoff_manifests(payload(objects, events)))
    assert [m["seq"] for m in res["manifests"]] == [15, 30]
    rows = res["manifests"][1]["objects"]
    assert [row["id"] for row in rows] == ["10", "2", "b"]


def test_no_handoffs_gives_empty_manifest_list():
    objects = [container("A"), item("I1")]
    res = ok(handoff_manifests(payload(objects, [move(1, [("I1", "A")]), seal(2, "A")])))
    assert res["manifests"] == []
    res = ok(handoff_manifests(payload(objects, [])))
    assert res["manifests"] == []


def test_events_replay_in_seq_order_not_array_order():
    objects = [container("A"), item("I1")]
    ordered = [move(1, [("I1", "A")]), seal(2, "A"), handoff(3, "A")]
    scrambled = [handoff(3, "A"), seal(2, "A"), move(1, [("I1", "A")])]
    assert handoff_manifests(payload(objects, ordered)) == handoff_manifests(
        payload(objects, scrambled)
    )
    # seq order decides which record fails
    events = [handoff(9, "A"), move(4, [("I1", "I1")])]
    res = handoff_manifests(payload(objects, events))
    assert res["error"]["seq"] == 4  # the move at seq 4 breaks first


# ---------------------------------------------------------------------------
# structural invalidity
# ---------------------------------------------------------------------------


def test_invalid_catalog_entries():
    cases = [
        {"objects": [{"id": "A", "kind": "box"}], "events": []},
        {"objects": [{"id": "", "kind": "item", "material_id": "M"}], "events": []},
        {"objects": [{"id": "I", "kind": "item"}], "events": []},
        {"objects": [{"id": "I", "kind": "item", "material_id": ""}], "events": []},
        {"objects": [{"id": "A", "kind": "container"}, {"id": "A", "kind": "container"}], "events": []},
        {"objects": "nope", "events": []},
        {"objects": [], "events": "nope"},
        ["not", "a", "dict"],
    ]
    for case in cases:
        invalid(handoff_manifests(case))
    res = handoff_manifests(
        {"objects": [{"id": "A", "kind": "container"}, {"id": "A", "kind": "item", "material_id": "M"}], "events": []}
    )
    (error,) = invalid(res)
    assert error["code"] == "duplicate_object"
    assert error["input_indices"] == [0, 1]


def test_invalid_event_records():
    objects = [container("A")]
    cases = [
        [handoff(1, "A"), handoff(1, "A")],  # duplicate seq
        [{"type": "handoff", "id": "A"}],  # missing seq
        [{"seq": -1, "type": "handoff", "id": "A"}],
        [{"seq": 1.5, "type": "handoff", "id": "A"}],
        [{"seq": True, "type": "handoff", "id": "A"}],
        [{"seq": 1, "type": "lift", "id": "A"}],
        [{"seq": 1, "type": "move", "changes": []}],
        [{"seq": 1, "type": "move"}],
        [{"seq": 1, "type": "move", "changes": [{"id": "A"}]}],  # parent_id key required
        [{"seq": 1, "type": "move", "changes": [{"id": "A", "parent_id": 3}]}],
        [{"seq": 1, "type": "seal", "id": "A"}],  # missing seal_id
        [{"seq": 1, "type": "open", "id": "", "seal_id": "S"}],
        [{"seq": 1, "type": "handoff"}],  # missing id
        ["not-an-object"],
    ]
    for events in cases:
        res = handoff_manifests(payload(objects, events))
        invalid(res)
    res = handoff_manifests(payload(objects, [handoff(1, "A"), handoff(1, "A")]))
    (error,) = invalid(res)
    assert error["code"] == "duplicate_seq" and error["input_indices"] == [0, 1]
    # seq is reported when readable even for structural problems
    res = handoff_manifests(payload(objects, [{"seq": 7, "type": "move", "changes": []}]))
    (error,) = invalid(res)
    assert error["seq"] == 7 and error["input_index"] == 0


def test_failure_delivers_no_partial_manifests():
    objects = [container("A"), container("B"), item("I1")]
    events = [
        move(1, [("I1", "A")]),
        seal(2, "A"),
        handoff(3, "A"),  # a perfectly good manifest exists...
        handoff(4, "B"),  # ...but this record is invalid
    ]
    res = handoff_manifests(payload(objects, events))
    assert res["status"] == "error"
    assert "manifests" not in res


# ---------------------------------------------------------------------------
# sample files + CLI
# ---------------------------------------------------------------------------


def test_sample_file_matches_expected():
    here = os.path.dirname(os.path.abspath(__file__))
    examples = os.path.join(os.path.dirname(here), "examples")
    with open(os.path.join(examples, "sample_manifests_input.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    with open(os.path.join(examples, "sample_manifests_expected.json"), encoding="utf-8") as fh:
        expected = json.load(fh)
    assert handoff_manifests(data) == expected


def test_cli_stdin_matches_file_output():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample = os.path.join(root, "examples", "sample_manifests_input.json")
    with open(sample, encoding="utf-8") as fh:
        text = fh.read()
    from_file = subprocess.run(
        [sys.executable, "-m", "ledger_pagination", "handoff-manifests", sample],
        cwd=root, capture_output=True, text=True,
    )
    from_stdin = subprocess.run(
        [sys.executable, "-m", "ledger_pagination", "handoff-manifests"],
        cwd=root, input=text, capture_output=True, text=True,
    )
    assert from_file.returncode == 0 and from_stdin.returncode == 0
    assert json.loads(from_file.stdout) == json.loads(from_stdin.stdout)
    bad = subprocess.run(
        [sys.executable, "-m", "ledger_pagination", "handoff-manifests"],
        cwd=root,
        input=json.dumps(payload([container("A")], [handoff(1, "A")])),
        capture_output=True, text=True,
    )
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["status"] == "error"


# ---------------------------------------------------------------------------
# randomized cross-check against the naive reference
# ---------------------------------------------------------------------------


def random_case(rng):
    """A random scan session: mostly valid records, sometimes a broken one.

    The generator keeps a shadow state so valid records stay valid under the
    full two-phase move semantics; a deliberately broken record ends the
    session (later records would never be reached by either implementation).
    """
    n_containers = rng.randint(2, 6)
    n_items = rng.randint(1, 8)
    containers = ["C%d" % i for i in range(n_containers)]
    items = ["I%d" % i for i in range(n_items)]
    objects = [container(c) for c in containers]
    objects += [item(i, rng.choice(["M1", "M2", "M3"])) for i in items]
    rng.shuffle(objects)

    parent = {oid: None for oid in containers + items}
    seals = {c: None for c in containers}
    departed = set()
    events = []
    seq = 0
    seal_no = 0

    def live(oid):
        return oid not in departed

    def chain_sealed(start, pmap):
        node = start
        while node is not None:
            if node in seals and seals[node] is not None:
                return True
            node = pmap[node]
        return False

    def group_valid(changes):
        seen = set()
        for x, pid in changes:
            if x in seen or not live(x):
                return False
            seen.add(x)
            if pid is not None and (pid not in seals or not live(pid)):
                return False
            if pid == parent[x]:
                return False
        for x, _pid in changes:
            old = parent[x]
            if old is not None and chain_sealed(old, parent):
                return False
        newparent = dict(parent)
        for x, pid in changes:
            newparent[x] = pid
        for x, _pid in changes:
            node = x
            walked = set()
            while node is not None and node not in walked:
                walked.add(node)
                node = newparent[node]
            if node is not None:
                return False
        for _x, pid in changes:
            if pid is not None and chain_sealed(pid, newparent):
                return False
        return True

    def apply_group(changes):
        for x, pid in changes:
            parent[x] = pid

    def subtree(root):
        out = []
        stack = [root]
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(o for o, p in parent.items() if p == node)
        return out

    def valid_move():
        for _attempt in range(30):
            k = rng.randint(1, 3)
            changes = []
            picked = set()
            candidates = [o for o in containers + items if live(o)]
            rng.shuffle(candidates)
            for x in candidates:
                if len(changes) >= k:
                    break
                if x in picked:
                    continue
                dests = [None] + [c for c in containers if live(c) and c != x]
                pid = rng.choice(dests)
                changes.append((x, pid))
                picked.add(x)
            if changes and group_valid(changes):
                return changes
        return None

    def broken_event():
        """A record very likely to be rejected; ends the session."""
        live_containers = [c for c in containers if live(c)]
        live_items = [i for i in items if live(i)]
        choice = rng.randint(0, 11)
        if choice == 0:
            return move(seq, [("NOPE", None)])
        if choice == 1 and live_items:
            return move(seq, [(live_items[0], live_items[0])])
        if choice == 2 and live_containers:
            c = live_containers[0]
            return move(seq, [(c, None), (c, None)])
        if choice == 3 and departed:
            return move(seq, [(sorted(departed)[0], None)])
        if choice == 4 and live_containers:
            c = rng.choice(live_containers)
            return seal(seq, c, seals[c] or "S") if seals[c] else seal(seq, c)
        if choice == 5 and live_containers:
            return open_(seq, rng.choice(live_containers), "WRONG")
        if choice == 6 and live_containers:
            unsealed = [c for c in live_containers if seals[c] is None]
            if unsealed:
                return handoff(seq, rng.choice(unsealed))
            return handoff(seq, live_containers[0])
        if choice == 7 and live_items:
            return handoff(seq, live_items[0])
        if choice == 8 and len(live_containers) >= 2:
            a, b = rng.sample(live_containers, 2)
            return move(seq, [(a, b), (b, a)])
        if choice == 9 and live_containers:
            sealed_with_content = [
                c for c in live_containers
                if seals[c] is not None and any(parent[o] == c for o in parent if live(o))
            ]
            if sealed_with_content:
                c = sealed_with_content[0]
                inner = [o for o in parent if parent[o] == c and live(o)][0]
                return move(seq, [(inner, None)])
            return move(seq, [("NOPE", None)])
        if choice == 10 and departed:
            return handoff(seq, sorted(departed)[0])
        return seal(seq, "NOPE")

    n_events = rng.randint(3, 30)
    broken_at = None if rng.random() < 0.35 else rng.randint(1, n_events)
    for i in range(n_events):
        seq += rng.randint(1, 3)
        if broken_at is not None and i + 1 == broken_at:
            events.append(broken_event())
            break
        kind_roll = rng.random()
        if kind_roll < 0.45:
            changes = valid_move()
            if changes is None:
                continue
            events.append(move(seq, changes))
            apply_group(changes)
        elif kind_roll < 0.65:
            candidates = [
                c for c in containers
                if live(c) and seals[c] is None
                and (parent[c] is None or not chain_sealed(parent[c], parent))
            ]
            if not candidates:
                continue
            c = rng.choice(candidates)
            seal_no += 1
            seals[c] = "S%d" % seal_no
            events.append(seal(seq, c, seals[c]))
        elif kind_roll < 0.80:
            candidates = [
                c for c in containers
                if live(c) and seals[c] is not None
                and (parent[c] is None or not chain_sealed(parent[c], parent))
            ]
            if not candidates:
                continue
            c = rng.choice(candidates)
            events.append(open_(seq, c, seals[c]))
            seals[c] = None
        else:
            candidates = [
                c for c in containers
                if live(c) and parent[c] is None and seals[c] is not None
            ]
            if not candidates:
                continue
            c = rng.choice(candidates)
            events.append(handoff(seq, c))
            for oid in subtree(c):
                departed.add(oid)
    rng.shuffle(events)
    return {"objects": objects, "events": events}


def test_randomized_cross_check():
    compared = 0
    for seed in range(1500):
        case = random_case(random.Random(seed))
        fast = handoff_manifests(case)
        try:
            ref = ref_manifests(case)
        except RefError as failure:
            assert fast["status"] == "error", (seed, fast)
            assert fast["error"]["code"] == failure.code, (seed, fast, failure.code)
            continue
        assert fast["status"] == "ok", (seed, fast)
        assert fast["manifests"] == ref["manifests"], seed
        compared += 1
    assert compared > 300  # clean sessions (no broken record) all match


# ---------------------------------------------------------------------------
# scale: deep nesting, long re-parenting runs, enumeration only at handoff
# ---------------------------------------------------------------------------


def test_deep_nesting_and_whole_box_runs():
    depth = 4000
    items = 4000
    objects = [container("K%d" % i) for i in range(depth)]
    objects += [item("I%d" % i) for i in range(items)]
    objects.append(container("HOLD"))
    events = []
    seq = 0
    # one deep chain K0 > K1 > ... > K{depth-1}
    for i in range(1, depth):
        seq += 1
        events.append(move(seq, [("K%d" % i, "K%d" % (i - 1))]))
    # fill the innermost box with many items
    seq += 1
    events.append(move(seq, [("I%d" % i, "K%d" % (depth - 1)) for i in range(items)]))
    # bounce the whole consignment between the deep chain top and HOLD
    rounds = 2000
    for r in range(rounds):
        seq += 1
        events.append(move(seq, [("K%d" % (depth - 1), "HOLD" if r % 2 else "K0")]))
    seq += 1
    events.append(move(seq, [("K%d" % (depth - 1), "K0")]))
    seq += 1
    events.append(seal(seq, "K0", "SEAL"))
    seq += 1
    events.append(handoff(seq, "K0"))
    started = time.perf_counter()
    res = handoff_manifests(payload(objects, events))
    elapsed = time.perf_counter() - started
    assert res["status"] == "ok", res.get("error")
    (manifest,) = res["manifests"]
    assert len(manifest["objects"]) == depth + items
    stats = res["stats"]
    assert stats["manifest_objects"] == depth + items
    # every access/link/cut is O(log n) amortized: rotations stay far below
    # any per-detail walk of the (up to 4000-deep) ancestor chain
    import math

    budget = 6 * stats["splays"] * (math.log2(depth + items) + 2)
    assert stats["splay_rotations"] <= budget, stats
    assert elapsed < 20, elapsed


def test_repeated_handoffs_of_many_small_units():
    units = 300
    objects = []
    events = []
    seq = 0
    for u in range(units):
        objects.append(container("U%04d" % u))
        objects.append(item("P%04d" % u))
        seq += 1
        events.append(move(seq, [("P%04d" % u, "U%04d" % u)]))
        seq += 1
        events.append(seal(seq, "U%04d" % u, "S%04d" % u))
    for u in range(units):
        seq += 1
        events.append(handoff(seq, "U%04d" % u))
    res = ok(handoff_manifests(payload(objects, events)))
    assert len(res["manifests"]) == units
    assert [m["seq"] for m in res["manifests"]] == sorted(m["seq"] for m in res["manifests"])
    total = sum(len(m["objects"]) for m in res["manifests"])
    assert total == 2 * units
