"""Tests against a real FalkorDB (docker compose): run `make test` or `docker compose exec api pytest -q`."""

from __future__ import annotations

import uuid

import pytest

from fally_graph import reader, writer
from fally_graph.db import UserGraph, list_users


@pytest.fixture
def make_user():
    made = []

    def _make(name="Test User"):
        uid = "test_" + uuid.uuid4().hex[:8]
        ug = UserGraph(uid)
        writer.ensure_self(ug, name)
        made.append(ug)
        return ug

    yield _make
    for ug in made:
        ug.drop()


def block(sid, ts, nodes=(), edges=(), kind="meeting"):
    return {"source": {"type": kind, "id": sid, "ts": ts, "title": sid},
            "graph": {"nodes": list(nodes), "edges": list(edges)}}


def person(tmp, name, quote=None, keys=(), aliases=(), props=None):
    return {"tmp": tmp, "type": "person", "name": name, "quote": quote, "keys": list(keys), "aliases": list(aliases),
            "props": props or {}}


def org(tmp, name):
    return {"tmp": tmp, "type": "organisation", "name": name}


def edge(src, dst, type, quote=None, confidence=0.8, valid_from=None):
    return {"src": src, "dst": dst, "type": type, "quote": quote, "confidence": confidence, "valid_from": valid_from}


def ppl(ug):
    return {p["name"]: p for p in reader.people(ug)}


# ---------------------------------------------------------------- isolation

def test_one_graph_per_user_and_no_leaks(make_user):
    a, b = make_user("Alice"), make_user("Bob")
    writer.ingest(a, block("s1", "2026-10-01", [person("p", "Quinn Harper", "Quinn: hi")], [edge("self", "p", "knows")]))
    writer.ingest(b, block("s1", "2026-10-01", [person("p", "Quinn Harper", "Quinn: hey")]))
    # each user is its own FalkorDB graph
    assert a.name != b.name and {a.user_id, b.user_id} <= set(list_users())
    qa, qb = ppl(a)["Quinn Harper"], ppl(b)["Quinn Harper"]
    assert qa["uid"] != qb["uid"]  # same name, different users: never the same node
    # b's graph knows nothing of a's edge or a's node id
    assert reader.why(b, qa["uid"])["kind"] is None
    assert reader.neighbourhood(b, qa["uid"])["edges"] == []
    with pytest.raises(writer.GraphError):  # a's node id cannot be used in b's graph
        writer.ingest(b, block("s2", "2026-10-02", [], [edge("self", qa["uid"], "knows")]))
    ctx = reader.get_context(b, "What do I know about Quinn Harper?", ["people", "conversations"], 1)
    assert "Quinn: hi" not in ctx["text"]  # a's quote never shows up in b's context
    b.drop()
    assert ppl(a)["Quinn Harper"]  # dropping one user's graph leaves the other untouched


def test_bad_user_id_cannot_address_other_graphs():
    for bad in ("../x", "a b", "", "X" * 100, "fally_user_x;DROP"):
        with pytest.raises(ValueError):
            UserGraph(bad)


# ---------------------------------------------------------------- resolution

def test_strong_keys_and_same_as_candidates(make_user):
    ug = make_user()
    writer.ingest(ug, block("s1", "2026-10-01", [person("r", "Rohan Mehta", "Rohan Mehta joined", keys=["email:abc"])]))
    writer.ingest(ug, block("s2", "2026-10-02", [person("r", "Rohan", "Rohan: climbing?")], kind="whatsapp"))
    writer.ingest(ug, block("s3", "2026-10-03", [person("r", "R. Mehta", "R. Mehta", keys=["email:abc"])]))
    p = ppl(ug)
    assert set(p) == {"Rohan Mehta", "Rohan"}  # a shared first name is not enough; a shared key is
    assert p["Rohan"]["same_as"] == [p["Rohan Mehta"]["uid"]]  # the "same as?" chip
    assert "R. Mehta" in p["Rohan Mehta"]["aliases"]


def test_commitments_resolve_by_key_only(make_user):
    ug = make_user()
    c = {"tmp": "c", "type": "commitment", "name": "Send the deck", "keys": ["commitment:a"]}
    writer.ingest(ug, block("s1", "2026-10-01", [c]))
    writer.ingest(ug, block("s2", "2026-10-02", [{**c, "keys": ["commitment:b"]}]))
    n = ug.q("MATCH (c:Commitment) RETURN count(c) AS n")[0]["n"]
    assert n == 2  # same words, two different promises


# ---------------------------------------------------------------- functional edges

def test_functional_edge_closes_old_period_and_restarts(make_user):
    ug = make_user()
    writer.ingest(ug, block("s1", "2026-01-10", [person("t", "Tara Bell", keys=["k:t"]), org("a", "Acme")],
                            [edge("t", "a", "works_at", "Tara works at Acme", valid_from="2025-01-01")]))
    writer.ingest(ug, block("s2", "2026-06-10", [person("t", "Tara Bell", keys=["k:t"]), org("b", "Birch")],
                            [edge("t", "b", "works_at", "Tara joined Birch", 0.9, "2026-06-01")]))
    rows = ug.q("MATCH (:Person {name:'Tara Bell'})-[r:WORKS_AT]->(o) RETURN o.name AS org, r.valid_from AS vf, "
                "r.valid_to AS vt ORDER BY vf")
    assert rows == [{"org": "Acme", "vf": "2025-01-01T00:00:00Z", "vt": "2026-06-01T00:00:00Z"},
                    {"org": "Birch", "vf": "2026-06-01T00:00:00Z", "vt": None}]
    # back to Acme: Birch closes, Acme is a NEW period (the old one keeps its history)
    writer.ingest(ug, block("s3", "2026-09-10", [person("t", "Tara Bell", keys=["k:t"]), org("a", "Acme")],
                            [edge("t", "a", "works_at", "Tara is back at Acme", 0.9, "2026-09-01")]))
    rows = ug.q("MATCH (:Person {name:'Tara Bell'})-[r:WORKS_AT]->(o) RETURN o.name AS org, r.valid_to AS vt "
                "ORDER BY r.valid_from")
    assert [(r["org"], r["vt"]) for r in rows] == [("Acme", "2026-06-01T00:00:00Z"), ("Birch", "2026-09-01T00:00:00Z"),
                                                   ("Acme", None)]
    current = ug.q("MATCH (:Person {name:'Tara Bell'})-[r:WORKS_AT]->(o) WHERE r.valid_to IS NULL RETURN o.name AS o")
    assert current == [{"o": "Acme"}]


def test_weaker_claim_is_contested_not_current(make_user):
    ug = make_user()
    writer.ingest(ug, block("s1", "2026-01-10", [person("t", "Tara Bell", keys=["k:t"]), org("a", "Acme")],
                            [edge("t", "a", "works_at", "Tara works at Acme", 0.9)]))
    writer.ingest(ug, block("s2", "2026-02-10", [person("t", "Tara Bell", keys=["k:t"]), org("b", "Birch")],
                            [edge("t", "b", "works_at", "maybe Birch?", 0.3)]))
    cur = ug.q("MATCH (:Person)-[r:WORKS_AT]->(o) WHERE r.valid_to IS NULL RETURN o.name AS o")
    assert cur == [{"o": "Acme"}]
    contested = ug.q("MATCH (:Person)-[r:WORKS_AT]->(o) WHERE r.contested = true RETURN o.name AS o")
    assert contested == [{"o": "Birch"}]


def test_weight_grows_once_per_source(make_user):
    ug = make_user()
    b1 = block("s1", "2026-01-10", [person("t", "Tara Bell", keys=["k:t"])], [edge("self", "t", "knows", "hi Tara")])
    writer.ingest(ug, b1)
    writer.ingest(ug, b1)  # the same source again: idempotent
    writer.ingest(ug, block("s2", "2026-01-11", [person("t", "Tara Bell", keys=["k:t"])], [edge("self", "t", "knows", "yo")]))
    w = ug.q("MATCH (:Self)-[r:KNOWS]->() RETURN r.weight AS w")[0]["w"]
    assert w == 2.0
    uid = ug.q("MATCH (:Self)-[r:KNOWS]->() RETURN r.uid AS u")[0]["u"]
    assert {e["quote"] for e in reader.why(ug, uid)["evidence"]} == {"hi Tara", "yo"}


# ---------------------------------------------------------------- merge / undo

def test_merge_and_undo_restore_exactly(make_user):
    ug = make_user()
    writer.ingest(ug, block("s1", "2026-10-01", [person("a", "Sam Ortiz", "Sam Ortiz here", keys=["k:1"]), org("o", "Acme")],
                            [edge("a", "o", "works_at", "Sam at Acme"), edge("self", "a", "knows", "met Sam")]))
    writer.ingest(ug, block("s2", "2026-10-02", [person("b", "Sammy", "Sammy: hey", keys=["k:2"]), org("o", "Acme")],
                            [edge("b", "o", "works_at", "Sammy at Acme too"), edge("self", "b", "knows", "Sammy!")],
                            kind="whatsapp"))

    def state():
        return sorted(tuple(r.values()) for r in ug.q(
            "MATCH (a:Entity)-[r]->(b:Entity) RETURN a.uid AS a, type(r) AS t, b.uid AS b, r.uid AS u, r.weight AS w"))

    p = ppl(ug)
    a, b = p["Sam Ortiz"]["uid"], p["Sammy"]["uid"]
    before = state()
    before_mentions = sorted(tuple(r.values()) for r in ug.q("MATCH (m:Mention) RETURN m.uid AS u, m.target_uid AS t"))
    mid = writer.merge(ug, b, a)
    p = ppl(ug)
    assert set(p) == {"Sam Ortiz"} and "Sammy" in p["Sam Ortiz"]["aliases"]
    w = ug.q("MATCH (:Person {uid:$a})-[r:WORKS_AT]->() RETURN r.weight AS w", {"a": a})
    assert w == [{"w": 2.0}]  # duplicate edges folded, not doubled
    assert len(reader.why(ug, a)["evidence"]) == 2  # both quotes now point at the survivor
    writer.undo_merge(ug, mid)
    assert state() == before
    after_mentions = sorted(tuple(r.values()) for r in ug.q("MATCH (m:Mention) RETURN m.uid AS u, m.target_uid AS t"))
    assert after_mentions == before_mentions
    assert set(ppl(ug)) == {"Sam Ortiz", "Sammy"}
    with pytest.raises(writer.GraphError):
        writer.undo_merge(ug, mid)


def test_never_merge_self(make_user):
    ug = make_user()
    writer.ingest(ug, block("s1", "2026-10-01", [person("a", "Sam Ortiz")]))
    me = writer.self_uid(ug)
    with pytest.raises(writer.GraphError):
        writer.merge(ug, ppl(ug)["Sam Ortiz"]["uid"], me)


# ---------------------------------------------------------------- neighbourhood / path

def _hub(ug):
    """Two people who only share the user (Self) and a third reachable through an organisation."""
    writer.ingest(ug, block("s1", "2026-10-01",
                            [person("x", "Xia Long"), person("y", "Yuri Petrov"), person("z", "Zed Amari"),
                             org("o", "Orbit")],
                            [edge("self", "x", "knows"), edge("self", "y", "knows"),
                             edge("x", "o", "works_at"), edge("z", "o", "works_at")]))
    return ppl(ug)


def test_neighbourhood_never_walks_through_self(make_user):
    ug = make_user()
    p = _hub(ug)
    nb = reader.neighbourhood(ug, p["Xia Long"]["uid"], depth=2)
    names = {n["name"] for n in nb["nodes"]}
    assert "Zed Amari" in names  # through Orbit: fine
    assert "Yuri Petrov" not in names  # only through Self: never
    assert "Test User" in names  # Self itself may be a direct neighbour


def test_path_is_bounded_and_avoids_self(make_user):
    ug = make_user()
    p = _hub(ug)
    path = reader.path(ug, p["Xia Long"]["uid"], p["Zed Amari"]["uid"])
    assert path["hops"] == 2 and [n["name"] for n in path["nodes"]] == ["Xia Long", "Orbit", "Zed Amari"]
    assert reader.path(ug, p["Xia Long"]["uid"], p["Yuri Petrov"]["uid"]) is None  # only via Self


# ---------------------------------------------------------------- open between / privacy / forget

def _promises(ug):
    writer.ingest(ug, block("s1", "2026-10-01", [
        person("k", "Kai Moreno", "Kai: deal"),
        {"tmp": "c1", "type": "commitment", "name": "Send Kai the contract", "keys": ["c:1"],
         "props": {"due": "2026-10-20", "status": "open"}},
        {"tmp": "c2", "type": "commitment", "name": "Kai to review the budget", "keys": ["c:2"],
         "props": {"status": "open"}},
    ], [edge("self", "c1", "promised", "I'll send the contract"), edge("c1", "k", "owed_by"),
        edge("k", "c2", "promised", "Kai: I'll review it"), edge("c2", "self", "owed_by")]))
    return ppl(ug)["Kai Moreno"]["uid"]


def test_open_between(make_user):
    ug = make_user()
    kai = _promises(ug)
    rows = reader.open_between(ug, kai)
    assert [(r["direction"], r["text"]) for r in rows] == [("you_owe", "Send Kai the contract"),
                                                           ("they_owe", "Kai to review the budget")]


def test_privacy_filter_categories_and_sens(make_user):
    ug = make_user()
    writer.ingest(ug, block("w1", "2026-10-01", [person("j", "Jules Park", "Jules: dinner?", props={"label": "girlfriend"})],
                            [edge("self", "j", "knows", "my girlfriend Jules")], kind="whatsapp"))
    writer.ingest(ug, block("m1", "2026-10-02", [person("v", "Vik Rao", "Vik: ship it", keys=["k:v"]), org("o", "Acme")],
                            [edge("v", "o", "works_at", "Vik works at Acme")], kind="meeting"))
    # meeting facts pass with the default categories
    ctx = reader.get_context(ug, "Where does Vik Rao work?", ["people", "conversations"], 1)
    assert any("Vik Rao works at Acme" in i["text"] for s in ctx["sections"] for i in s["items"])
    assert all(set(i["categories"]) <= {"people", "conversations"} for s in ctx["sections"] for i in s["items"])
    assert all(i.get("source") for s in ctx["sections"] for i in s["items"] if i["kind"] == "fact")  # cited
    # whatsapp off: nothing about Jules leaves, not even the name
    ctx = reader.get_context(ug, "What about Jules Park?", ["people", "conversations"], 1)
    assert "Jules" not in ctx["text"].replace("What about Jules Park?", "") and ctx["filtered_out"] > 0
    # whatsapp on but relationships off: the intimate edge (sens 2) stays out
    ctx = reader.get_context(ug, "What about Jules Park?", ["people", "whatsapp", "conversations"], 2)
    assert "girlfriend" not in ctx["text"]
    # a relationship question without the switch gets the switch note and no hint of who
    ctx = reader.get_context(ug, "Who is my girlfriend?", ["people", "whatsapp"], 1)
    assert "Close relationships" in ctx["text"] and "Jules" not in ctx["text"]
    # with relationships on and tier 2 it is shared
    ctx = reader.get_context(ug, "What about Jules Park?", ["people", "whatsapp", "relationships"], 2)
    assert "girlfriend" in ctx["text"]


def test_forget_person_removes_node_edges_quotes_and_promises(make_user):
    ug = make_user()
    kai = _promises(ug)
    out = writer.forget_person(ug, kai)
    assert out["nodes_deleted"] == 3  # Kai and the two promises only Kai was part of
    left = ug.q("MATCH (m:Mention) RETURN m.quote AS q")
    assert all("Kai" not in (r["q"] or "") and "contract" not in (r["q"] or "") for r in left)
    assert "Kai Moreno" not in ppl(ug)
    assert ug.q("MATCH (s:Source) RETURN count(s) AS n")[0]["n"] == 0  # no quotes left: the source goes too


# ---------------------------------------------------------------- synthetic data end to end

def test_synthetic_seed_end_to_end():
    from synth.load import load_user
    load_user("asha")
    ug = UserGraph("asha")
    p = ppl(ug)
    assert p["Rohan"]["same_as"] == [p["Rohan Mehta"]["uid"]]
    tomas_jobs = ug.q("MATCH (:Person {name:'Tomas Reyes'})-[r:WORKS_AT]->(o) RETURN o.name AS o, r.valid_to AS vt "
                      "ORDER BY r.valid_from")
    assert tomas_jobs[-1] == {"o": "Northwind Bikes", "vt": None} and tomas_jobs[0]["vt"] is not None
    ctx = reader.get_context(ug, "What's open between me and Tomas?", None, 1)
    open_items = [i["text"] for s in ctx["sections"] if s["title"] == "Open" for i in s["items"]]
    assert any("pricing deck" in t for t in open_items) and any("Kofi" in t for t in open_items)


def test_get_context_asks_which_person_for_a_shared_first_name():
    from synth.load import load_user
    load_user("asha")
    ug = UserGraph("asha")
    ctx = reader.get_context(ug, "What do I know about Rohan?", ["people", "whatsapp", "calendar"], 1)
    assert ctx["which_person"] == ["Rohan", "Rohan Mehta"]
    ctx = reader.get_context(ug, "What do I know about Rohan Mehta?", ["people", "whatsapp", "calendar"], 1)
    assert ctx["which_person"] is None and [p["name"] for p in ctx["resolved"]["people"]] == ["Rohan Mehta"]


def test_mcp_tool_and_http_api():
    from fastapi.testclient import TestClient
    from fally_graph.api import app
    from synth.load import load_user
    load_user("dev")
    c = TestClient(app)
    tools = c.post("/mcp/dev", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert tools["result"]["tools"][0]["name"] == "get_context"
    r = c.post("/mcp/dev", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "get_context", "arguments": {"question": "Where does Oren live?"}}}).json()
    text = r["result"]["content"][0]["text"]
    assert "Oren Blum lives in Denver" in text and "lived in Austin" in text
    assert "Zara" not in text  # family whatsapp chat: whatsapp is off for this connection
    assert c.get("/api/users/nobody/people").status_code == 404
    assert c.get("/api/users/..%2Fx/people").status_code in (400, 404)


def test_responses_show_the_falkordb_queries_that_ran():
    from fastapi.testclient import TestClient
    from fally_graph.api import app
    from synth.load import load_user
    load_user("asha")
    c = TestClient(app)
    h = c.get("/api/health").json()
    assert h["graph_db"]["engine"] == "FalkorDB" and h["graph_db"]["graphs"] >= 1 and "version" in h["graph_db"]
    r = c.post("/api/users/asha/context", json={"question": "What is open between me and Tomas?"})
    assert int(r.headers["X-FalkorDB-Queries"]) >= 1
    fq = r.json()["falkordb"]
    assert fq and all(q["graph"] == "fally_user_asha" for q in fq)
    assert any("MATCH" in q["cypher"] for q in fq)
