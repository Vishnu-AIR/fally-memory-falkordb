"""The only code that writes the memory graph.

ingest() takes an extractor-style graph block:
  {source: {type, id, ts, title},
   graph: {nodes: [{tmp, type, name, keys, aliases, props, quote}],
           edges: [{src, dst, type, confidence, basis, valid_from, props, quote}]}}
src/dst are a tmp id from the same block, "self", or the uid of an existing node.

Rules (re-implemented from Fally's Postgres graph writer):
  * resolution needs strong evidence: a shared key (email/phone hash, contact id, speaker id), an exact multi-word
    name or alias, or (non-person types) the exact normalised name. A shared single first name is never enough:
    the new node is created and carries the old one as a "same as?" candidate (same_as);
  * commitments and decisions with keys resolve by keys only (the same words can be two different promises);
  * functional edges (works_at, lives_in, reports_to): one current destination per (src, type). A new destination
    closes the weaker current period at the new start; a stronger current edge makes the claim "contested"
    (stored as a zero-length period that never shows as current); a claim older than the current period is stored
    as a past period; A -> B -> A restarts A as a new period;
  * every node and edge points back to the words: Mention nodes linked to a Source;
  * weight grows once per distinct source; confidence keeps the max; the stronger basis wins;
  * merge() is undoable: it stores a snapshot that undo_merge() uses to restore edges, quotes and aliases.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import model as M, queries as Q
from .db import UserGraph, sub


class GraphError(ValueError):
    pass


@dataclass
class Source:
    source_id: str
    source_type: str
    ts: str
    title: str = ""

    @property
    def categories(self) -> list[str]:
        return list(M.SOURCE_CATEGORIES.get(self.source_type, []))


@dataclass
class IngestResult:
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: list[dict] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- helpers

def ensure_self(ug: UserGraph, display_name: str = "Me") -> str:
    ug.ensure_schema()
    row = ug.one(Q.ENSURE_SELF, {"uid": M.new_id("self"), "name": display_name, "norm": M.norm(display_name),
                                 "now": M.now_iso()})
    return row["uid"]


def self_uid(ug: UserGraph) -> str:
    row = ug.one(Q.GET_SELF)
    if not row:
        raise GraphError("user graph has no self node")
    return row["uid"]


def canonical(ug: UserGraph, uid: str) -> str | None:
    seen = set()
    cur = uid
    while cur and cur not in seen:
        seen.add(cur)
        row = ug.one(Q.GET_NODE, {"uid": cur})
        if not row:
            return None
        if not row["merged_into"]:
            return cur
        cur = row["merged_into"]
    return None


def _node_categories(node_type: str, src: Source, sens: int) -> list[str]:
    cats = set(src.categories)
    if node_type == "person":
        cats.add("people")
    if node_type in ("commitment",):
        cats.add("plans")
    if sens >= 2:
        cats.add("relationships")
    return sorted(cats)


def _edge_categories(edge_type: str, src_type: str, dst_type: str, src: Source, sens: int) -> list[str]:
    cats = set(src.categories)
    if "person" in (src_type, dst_type):
        cats.add("people")
    if edge_type in M.PLAN_EDGES:
        cats.add("plans")
    if "self" in (src_type, dst_type) and "person" not in (src_type, dst_type):
        cats.add("about_me")
    if sens >= 2:
        cats.add("relationships")
    return sorted(cats)


# ---------------------------------------------------------------- resolution

def resolve(ug: UserGraph, node: dict) -> tuple[str | None, list[str], str]:
    """(existing uid or None, "same as?" candidates, how). Strong evidence only."""
    typ, name = node["type"], node["name"]
    keys = M.strs(node.get("keys"))
    aliases = M.clean_aliases(typ, name, node.get("aliases") or [])
    if keys:
        hits = ug.q(Q.NODES_BY_KEYS, {"type": typ, "keys": keys})
        if hits:
            return hits[0]["uid"], [], "key"
        if typ in M.KEY_ONLY_TYPES:
            return None, [], "new"
    if typ in M.KEY_ONLY_TYPES:
        return None, [], "new"
    names = sorted({M.norm(name), *(M.norm(a) for a in aliases)} - {""})
    candidates: list[str] = []
    for hit in ug.q(Q.NODES_BY_NAMES, {"type": typ, "names": names}):
        if typ != "person":
            return hit["uid"], [], "name"
        how = M.name_match(name, aliases, hit["name"], hit["aliases"] or [])
        if how == "strong":
            return hit["uid"], [], "name"
        if how == "weak":
            candidates.append(hit["uid"])
    if typ == "person":
        # near spellings ("Rohan" / "Rohan Mehta", "Ines" / "Inez"): fuzzy full-text plus a first-name rule
        for hit in _fuzzy(ug, typ, name):
            if hit["uid"] in candidates:
                continue
            how = M.name_match(name, aliases, hit["name"], hit["aliases"] or [])
            close = M.edit_distance(M.norm(name), M.norm(hit["name"])) <= 1
            if how or close:
                candidates.append(hit["uid"])
    return None, candidates[:5], "new"


def _fuzzy(ug: UserGraph, typ: str, name: str) -> list[dict]:
    terms = [w for w in M.words(name) if len(w) >= 3]
    if not terms:
        return []
    q = "|".join(f"%{w}%" for w in terms)
    try:
        return ug.q(Q.FUZZY_NAMES, {"q": q, "type": typ})
    except Exception:
        # fall back to a scan if the full-text syntax is rejected
        return [r for r in ug.q(Q.ALL_ACTIVE_OF_TYPE, {"type": typ})
                if any(M.edit_distance(t, w) <= 1 for t in terms for w in M.words(r["name"]))]


# ---------------------------------------------------------------- nodes

def _write_node(ug: UserGraph, node: dict, src: Source) -> dict:
    typ = node.get("type")
    if typ not in M.NODE_TYPES or typ == "self":
        raise GraphError(f"bad node type {typ!r}")
    name = str(node.get("name") or "").strip()
    if not M.norm(name):
        raise GraphError("node without a name")
    node = {**node, "type": typ, "name": name}
    quote = (node.get("quote") or None)
    props = node.get("props") if isinstance(node.get("props"), dict) else {}
    sens = max(M.text_sens(quote, name, json.dumps(props)), int(props.get("sens", 0) or 0))
    cats = _node_categories(typ, src, sens)
    aliases = M.clean_aliases(typ, name, node.get("aliases") or [])
    keys = M.strs(node.get("keys"))
    uid, candidates, how = resolve(ug, node)
    at = src.ts
    if uid:
        cur = ug.one(Q.GET_NODE, {"uid": uid})
        all_aliases = M.clean_aliases(typ, cur["name"], [*(cur["aliases"] or []), *aliases,
                                                          *([name] if M.norm(name) != M.norm(cur["name"]) else [])])
        all_keys = M.union(cur["keys"] or [], keys)
        names_norm = sorted({M.norm(cur["name"]), *(M.norm(a) for a in all_aliases)})
        merged_props = {**json.loads(cur["props"] or "{}"), **props}
        ug.q(Q.TOUCH_NODE, {"uid": uid, "aliases": all_aliases, "keys": all_keys, "names_norm": names_norm,
                            "search_text": " ".join([cur["name"], *all_aliases]), "at": at,
                            "categories": M.union(cur["categories"] or [], cats), "sens": sens,
                            "props_json": json.dumps(merged_props, sort_keys=True)})
        _promote(ug, uid, merged_props)
    else:
        uid = M.new_id(typ[:4])
        fields = {
            "uid": uid, "type": typ, "name": name, "aliases": aliases, "keys": keys,
            "names_norm": sorted({M.norm(name), *(M.norm(a) for a in aliases)}),
            "search_text": " ".join([name, *aliases]), "props": json.dumps(props, sort_keys=True),
            "first_seen": at, "last_seen": at, "importance": 1.0, "same_as": candidates,
            "categories": cats, "sens": sens,
        }
        ug.q(sub(Q.CREATE_NODE, LABEL=M.label(typ)), {"props": fields})
        _promote(ug, uid, props)
    if quote and not _has_mention(ug, uid, src):
        ug.q(Q.NODE_MENTION, {"uid": M.new_id("m"), "target_uid": uid, "source_id": src.source_id,
                              "quote": str(quote)[:500], "ts": at, "sens": sens, "categories": cats})
    return {"uid": uid, "resolution": how, "same_as": candidates}


def _promote(ug: UserGraph, uid: str, props: dict) -> None:
    """Scalar props the readers filter on (due, status, label) become real node properties."""
    fields = {k: props[k] for k in ("due", "status", "label") if isinstance(props.get(k), (str, int, float))}
    if "due" in fields:
        fields["due"] = M.ts(fields["due"]) or str(fields["due"])
    if fields:
        ug.q(Q.SET_NODE_FIELDS, {"uid": uid, "fields": fields})


def _has_mention(ug: UserGraph, target_uid: str, src: Source) -> bool:
    row = ug.one(Q.MENTION_EXISTS, {"target_uid": target_uid, "source_id": src.source_id})
    return bool(row and row["n"])


# ---------------------------------------------------------------- edges

def upsert_edge(ug: UserGraph, src_uid: str, dst_uid: str, edge_type: str, src: Source, *,
                confidence: float = 0.5, basis: str = "inferred", valid_from: str | None = None,
                props: dict | None = None, quote: str | None = None, result: IngestResult | None = None) -> str | None:
    """The one rule for writing an edge. Returns the edge uid (None for a self-loop)."""
    R = M.rel(edge_type)
    a, b = canonical(ug, src_uid), canonical(ug, dst_uid)
    if not a or not b:
        raise GraphError("edge end is not a node of this user")
    if a == b:
        return None
    na, nb = ug.one(Q.GET_NODE, {"uid": a}), ug.one(Q.GET_NODE, {"uid": b})
    at = src.ts
    vf = M.ts(valid_from)
    basis = basis if basis in M.BASIS_RANK else "inferred"
    sens = max(M.text_sens(quote), int((props or {}).get("sens", 0) or 0), na["sens"] or 0, nb["sens"] or 0)
    cats = _edge_categories(edge_type, na["type"], nb["type"], src, sens)
    functional = edge_type in M.FUNCTIONAL_TYPES
    rows = ug.q(sub(Q.EDGES_OF_RELATION, REL=R), {"src": a, "dst": b})

    def started(r):  # had started by the observation time
        return (r["valid_from"] or "") <= at

    hit = next((r for r in rows if r["valid_to"] is None and started(r)), None)
    hit = hit or next((r for r in rows if r["valid_to"] is not None and started(r) and r["valid_to"] > at
                       and not r["contested"]), None)
    hit = hit or next((r for r in rows if r["valid_to"] is None), None)
    if functional and not hit:
        # a contested claim keeps collecting its own mentions
        hit = next((r for r in rows if r["contested"]), None)
        if hit:
            others = ug.q(sub(Q.OPEN_FUNCTIONAL_OTHERS, REL=R), {"src": a, "dst": b})
            if not any(o["confidence"] > confidence for o in others):
                hit = None  # the stronger edge is gone: the relation (re)starts below
    if hit:
        _remention(ug, R, hit, src, confidence, basis, vf, props, cats, sens)
        uid = hit["uid"]
    else:
        start = vf or (at if rows else None)
        fields: dict[str, Any] = {"confidence": confidence, "basis": basis, "weight": 1.0, "sources": [src.source_id],
                                  "categories": cats, "sens": sens, "props": json.dumps(props or {}, sort_keys=True),
                                  "created_at": M.now_iso(), "updated_at": M.now_iso(), "type": edge_type}
        if functional:
            others = ug.q(sub(Q.OPEN_FUNCTIONAL_OTHERS, REL=R), {"src": a, "dst": b})
            later = [o["valid_from"] for o in others if o["valid_from"] and o["valid_from"] > (vf or at)]
            if later:  # stale claim: a past period that ended where the current one starts
                fields.update(valid_from=vf, valid_to=min(later))
            else:
                close_at = start or at
                closed = 0
                for o in others:
                    if o["confidence"] <= confidence:
                        vt = max(close_at, o["valid_from"] or close_at)
                        ug.q(sub(Q.CLOSE_EDGE, REL=R), {"uid": o["uid"], "valid_to": vt, "now": M.now_iso()})
                        closed += 1
                        if result is not None:
                            result.closed.append(o["uid"])
                if closed and start is None:
                    start = close_at
                if any(o["confidence"] > confidence for o in others):
                    fields.update(valid_from=start or at, valid_to=start or at, contested=True)
                else:
                    fields["valid_from"] = start
        else:
            fields["valid_from"] = start
        fields = {k: v for k, v in fields.items() if v is not None}
        uid = M.new_id("e")
        fields["uid"] = uid
        ug.q(sub(Q.CREATE_EDGE, REL=R), {"src": a, "dst": b, "props": fields})
    if quote and not _has_mention(ug, uid, src):
        ug.q(Q.EDGE_MENTION, {"uid": M.new_id("m"), "target_uid": uid, "source_id": src.source_id,
                              "quote": str(quote)[:500], "ts": at, "sens": sens, "categories": cats})
    return uid


def _remention(ug, R, hit, src: Source, confidence, basis, vf, props, cats, sens):
    sources = list(hit["sources"] or [])
    fields: dict[str, Any] = {"updated_at": M.now_iso()}
    if src.source_id not in sources:
        sources.append(src.source_id)
        fields["weight"] = float(hit["weight"] or 1) + 1.0
        fields["sources"] = sources
    fields["confidence"] = max(float(hit["confidence"] or 0), confidence)
    if M.BASIS_RANK.get(basis, 0) > M.BASIS_RANK.get(hit["basis"] or "inferred", 0):
        fields["basis"] = basis
    if vf and not hit["valid_from"]:
        fields["valid_from"] = vf
    if props:
        fields["props"] = json.dumps({**json.loads(hit["props"] or "{}"), **props}, sort_keys=True)
    fields["categories"] = M.union(hit["categories"] or [], cats)
    fields["sens"] = max(int(hit["sens"] or 0), sens)
    ug.q(sub(Q.UPDATE_EDGE, REL=R), {"uid": hit["uid"], "fields": fields})


# ---------------------------------------------------------------- ingest

def ingest(ug: UserGraph, block: dict) -> IngestResult:
    s = block.get("source") or {}
    if s.get("type") not in M.SOURCE_TYPES or not s.get("id"):
        raise GraphError("source needs a known type and an id")
    src = Source(source_id=str(s["id"])[:120], source_type=s["type"], ts=M.ts(s.get("ts")) or M.now_iso(),
                 title=str(s.get("title") or "")[:200])
    graph = block.get("graph") or {}
    nodes, edges = graph.get("nodes") or [], graph.get("edges") or []
    if len(nodes) > 200 or len(edges) > 400:
        raise GraphError("block too large")
    res = IngestResult()
    with ug.lock:
        me = ensure_self(ug)
        ug.q(Q.UPSERT_SOURCE, {"source_id": src.source_id, "source_type": src.source_type, "title": src.title,
                               "ts": src.ts, "categories": src.categories})
        tmp: dict[str, str] = {"self": me}
        for n in nodes:
            out = _write_node(ug, n, src)
            tmp[str(n.get("tmp") or out["uid"])] = out["uid"]
            res.nodes[str(n.get("tmp") or out["uid"])] = out
        for e in edges:
            a = tmp.get(str(e.get("src"))) or e.get("src")
            b = tmp.get(str(e.get("dst"))) or e.get("dst")
            uid = upsert_edge(ug, str(a), str(b), str(e.get("type")), src,
                              confidence=M.confidence(e.get("confidence")), basis=str(e.get("basis") or "inferred"),
                              valid_from=e.get("valid_from"), props=e.get("props") if isinstance(e.get("props"), dict) else None,
                              quote=e.get("quote"), result=res)
            res.edges.append({"uid": uid, "type": e.get("type"), "src": a, "dst": b})
    return res


# ---------------------------------------------------------------- merge / undo

def merge(ug: UserGraph, from_uid: str, into_uid: str) -> str:
    """Fold `from` into `into` (the user said "same person"). Returns the merge id for undo."""
    with ug.lock:
        a, b = ug.one(Q.GET_NODE, {"uid": from_uid}), ug.one(Q.GET_NODE, {"uid": into_uid})
        if not a or not b or a["merged_into"] or b["merged_into"]:
            raise GraphError("both nodes must exist and not be merged")
        if from_uid == into_uid or "self" in (a["type"], b["type"]) or a["type"] != b["type"]:
            raise GraphError("can only merge two different nodes of the same type (never self)")
        snap: dict[str, Any] = {"into_before": {k: b[k] for k in ("aliases", "keys", "names_norm", "same_as",
                                                                 "search_text", "categories", "sens")},
                                "moved": [], "folded": [], "dropped": [], "node_mentions": []}
        for e in ug.q(Q.EDGES_OF_NODE.replace("{ENTITY_RELS}", M.ENTITY_RELS), {"uid": from_uid}):
            props = dict(e["props"])
            R = e["rel"]
            out = e["src"] == from_uid
            other = e["other"]
            rec = {"rel": R, "src": e["src"], "dst": e["dst"], "props": props}
            ug.q(sub(Q.DELETE_EDGE, REL=R), {"uid": props["uid"]})
            if other == into_uid:
                snap["dropped"].append(rec)
                continue
            ns, nd = (into_uid, other) if out else (other, into_uid)
            twins = ug.q(sub(Q.EDGES_OF_RELATION, REL=R), {"src": ns, "dst": nd})
            twin = next((t for t in twins if t["valid_from"] == props.get("valid_from")
                         and t["valid_to"] == props.get("valid_to")), None)
            if twin:
                before = {k: twin[k] for k in ("weight", "confidence", "sources", "categories", "sens", "basis")}
                srcs = M.union(twin["sources"] or [], props.get("sources") or [])
                ug.q(sub(Q.UPDATE_EDGE, REL=R), {"uid": twin["uid"], "fields": {
                    "weight": float(len(srcs)), "sources": srcs,
                    "confidence": max(twin["confidence"] or 0, props.get("confidence") or 0),
                    "categories": M.union(twin["categories"] or [], props.get("categories") or []),
                    "sens": max(twin["sens"] or 0, props.get("sens") or 0)}})
                moved = ug.one(Q.REPOINT_EDGE_MENTIONS, {"old": props["uid"], "new": twin["uid"]})["moved"]
                snap["folded"].append({**rec, "into_edge": twin["uid"], "into_before": before, "mentions": moved})
            else:
                ug.q(sub(Q.CREATE_EDGE, REL=R), {"src": ns, "dst": nd, "props": props})
                snap["moved"].append({**rec, "new_src": ns, "new_dst": nd})
        mids = ug.one(Q.NODE_MENTION_UIDS, {"uid": from_uid})["uids"]
        if mids:
            ug.q(Q.MOVE_NODE_MENTIONS, {"from": from_uid, "into": into_uid, "uids": mids})
        snap["node_mentions"] = mids
        aliases = M.clean_aliases(b["type"], b["name"], [*(b["aliases"] or []), a["name"], *(a["aliases"] or [])])
        ug.q(Q.SET_NODE_FIELDS, {"uid": into_uid, "fields": {
            "aliases": aliases, "keys": M.union(b["keys"] or [], a["keys"] or []),
            "names_norm": sorted({M.norm(b["name"]), *(M.norm(x) for x in aliases)}),
            "search_text": " ".join([b["name"], *aliases]),
            "same_as": [x for x in (b["same_as"] or []) if x != from_uid],
            "categories": M.union(b["categories"] or [], a["categories"] or []),
            "sens": max(b["sens"] or 0, a["sens"] or 0)}})
        ug.q(Q.SET_NODE_FIELDS, {"uid": from_uid, "fields": {"merged_into": into_uid,
                                                             "same_as": [x for x in (a["same_as"] or []) if x != into_uid],
                                                             "same_as_before": a["same_as"] or []}})
        mid = M.new_id("merge")
        ug.q(Q.CREATE_MERGE, {"uid": mid, "from": from_uid, "into": into_uid, "now": M.now_iso(),
                              "snapshot": json.dumps(snap)})
        return mid


def undo_merge(ug: UserGraph, merge_uid: str) -> dict:
    with ug.lock:
        mrow = ug.one(Q.GET_MERGE, {"uid": merge_uid})
        if not mrow or mrow["undone_at"]:
            raise GraphError("no such merge, or already undone")
        snap = json.loads(mrow["snapshot"])
        f, i = mrow["from_uid"], mrow["into_uid"]
        for rec in snap["moved"]:
            ug.q(sub(Q.DELETE_EDGE, REL=rec["rel"]), {"uid": rec["props"]["uid"]})
            ug.q(sub(Q.CREATE_EDGE, REL=rec["rel"]), {"src": rec["src"], "dst": rec["dst"], "props": rec["props"]})
        for rec in snap["folded"]:
            ug.q(sub(Q.UPDATE_EDGE, REL=rec["rel"]), {"uid": rec["into_edge"], "fields": rec["into_before"]})
            ug.q(sub(Q.CREATE_EDGE, REL=rec["rel"]), {"src": rec["src"], "dst": rec["dst"], "props": rec["props"]})
            if rec["mentions"]:
                ug.q(Q.REPOINT_MENTIONS_BY_UID, {"uids": rec["mentions"], "new": rec["props"]["uid"]})
        for rec in snap["dropped"]:
            ug.q(sub(Q.CREATE_EDGE, REL=rec["rel"]), {"src": rec["src"], "dst": rec["dst"], "props": rec["props"]})
        if snap["node_mentions"]:
            ug.q(Q.MOVE_NODE_MENTIONS, {"from": i, "into": f, "uids": snap["node_mentions"]})
        # restore the surviving node's identity fields; anything added after the merge is kept
        cur = ug.one(Q.GET_NODE, {"uid": i})
        before = snap["into_before"]
        fnode = ug.one(Q.GET_NODE, {"uid": f})
        added_aliases = {M.norm(fnode["name"]), *(M.norm(x) for x in fnode["aliases"] or [])} - \
                        {M.norm(x) for x in before["aliases"] or []}
        aliases = [x for x in cur["aliases"] or [] if M.norm(x) not in added_aliases]
        added_keys = set(fnode["keys"] or []) - set(before["keys"] or [])
        ug.q(Q.SET_NODE_FIELDS, {"uid": i, "fields": {
            "aliases": aliases, "keys": [k for k in cur["keys"] or [] if k not in added_keys],
            "names_norm": sorted({M.norm(cur["name"]), *(M.norm(x) for x in aliases)}),
            "search_text": " ".join([cur["name"], *aliases]),
            "same_as": M.union(cur["same_as"] or [], [f] if f in (before["same_as"] or []) else []),
            "categories": before["categories"], "sens": before["sens"]}})
        ug.q("MATCH (n:Entity {uid: $uid}) SET n.merged_into = NULL, n.same_as = n.same_as_before, "
             "n.same_as_before = NULL", {"uid": f})
        ug.q(Q.MARK_MERGE_UNDONE, {"uid": merge_uid, "now": M.now_iso()})
        return {"merge": merge_uid, "restored": f}


# ---------------------------------------------------------------- forget

def forget_person(ug: UserGraph, person_uid: str) -> dict:
    """Forget a person: the node, every edge touching it, the promises only they were part of, and every quote
    about any of these. Sources left with no quotes are deleted too."""
    with ug.lock:
        node = ug.one(Q.GET_NODE, {"uid": person_uid})
        if not node or node["type"] != "person":
            raise GraphError("no such person")
        commitments = ug.one(Q.FORGET_COMMITMENTS, {"uid": person_uid})["uids"]
        doomed_nodes = [person_uid, *commitments]
        # merged-away duplicates of this person go too
        doomed_nodes += [r["uid"] for r in ug.q("MATCH (n:Entity {merged_into: $uid}) RETURN n.uid AS uid",
                                                  {"uid": person_uid})]
        edge_uids: list[str] = []
        for n in doomed_nodes:
            edge_uids += ug.one(Q.FORGET_EDGE_UIDS, {"uid": n})["uids"]
        mentions = ug.one(Q.DELETE_MENTIONS_OF, {"uids": [*doomed_nodes, *edge_uids]})["n"]
        nodes = ug.one(Q.DELETE_NODES, {"uids": doomed_nodes})["n"]
        for n in doomed_nodes:
            ug.q(Q.CLEAR_SAME_AS, {"uid": n})
        sources = ug.one(Q.DELETE_ORPHAN_SOURCES)["n"]
        return {"forgotten": node["name"], "nodes_deleted": nodes, "edges_deleted": len(set(edge_uids)),
                "quotes_deleted": mentions, "sources_deleted": sources}
