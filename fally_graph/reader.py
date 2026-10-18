"""Readers: people, neighbourhood, path, why, open-between and get_context (the small cited slice an outside AI gets).

Privacy: every item that can leave the app carries `categories` (every category its sources need) and `sens`
(0 ordinary, 1 personal, 2 intimate). A caller keeps an item only when categories <= allowed and sens <= tier_max.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import model as M, queries as Q
from .db import UserGraph, sub

MAX_DEPTH = 2
MAX_HOPS = 4


def people(ug: UserGraph) -> list[dict]:
    return ug.ro(Q.PEOPLE)


def neighbourhood(ug: UserGraph, uid: str, depth: int = 2, limit: int = 300) -> dict:
    depth = max(1, min(MAX_DEPTH, int(depth)))
    edges = ug.ro(sub(Q.NEIGHBOURHOOD, DEPTH=depth), {"uid": uid, "limit": limit})
    uids = sorted({uid, *(e["src"] for e in edges), *(e["dst"] for e in edges)})
    nodes = ug.ro(Q.NODES_BY_UIDS, {"uids": uids})
    return {"center": uid, "depth": depth, "nodes": nodes, "edges": edges}


def path(ug: UserGraph, a: str, b: str, max_hops: int = MAX_HOPS) -> dict | None:
    """Bounded BFS: try 1 hop, then 2, ... and stop at the first level where b is found."""
    for hops in range(1, max(1, min(MAX_HOPS, int(max_hops))) + 1):
        rows = ug.ro(sub(Q.PATH_AT_DEPTH, HOPS=hops), {"a": a, "b": b})
        if rows:
            return {"hops": rows[0]["hops"], "nodes": rows[0]["nodes"], "edges": rows[0]["rels"]}
    return None


def why(ug: UserGraph, uid: str) -> dict:
    """Provenance of a fact (an edge) or of a node: the quotes and sources behind it."""
    edge = ug.ro(Q.FIND_EDGE, {"uid": uid})
    if edge:
        e = edge[0]
        p = e["props"]
        return {"kind": "edge", "uid": uid, "fact": f"{e['src_name']} {e['rel'].lower()} {e['dst_name']}",
                "valid_from": p.get("valid_from"), "valid_to": p.get("valid_to"), "confidence": p.get("confidence"),
                "basis": p.get("basis"), "weight": p.get("weight"), "contested": p.get("contested", False),
                "evidence": ug.ro(Q.WHY_EDGE, {"uid": uid})}
    node = ug.ro(Q.GET_NODE, {"uid": uid})
    if node:
        return {"kind": "node", "uid": uid, "fact": node[0]["name"], "evidence": ug.ro(Q.WHY_NODE, {"uid": uid})}
    return {"kind": None, "uid": uid, "evidence": []}


def open_between(ug: UserGraph, person_uid: str) -> list[dict]:
    return ug.ro(Q.OPEN_BETWEEN, {"uid": person_uid})


def overview(ug: UserGraph, limit: int = 400) -> dict:
    edges = ug.ro(Q.GRAPH_OVERVIEW.replace("{ENTITY_RELS}", M.ENTITY_RELS), {"limit": limit})
    uids = sorted({*(e["src"] for e in edges), *(e["dst"] for e in edges)})
    return {"nodes": ug.ro(Q.NODES_BY_UIDS, {"uids": uids}), "edges": edges}


# ---------------------------------------------------------------- get_context

_PLANS = re.compile(r"\b(promise|promised|owe|owes|waiting|pending|follow up|follow-up|todo|to do|due|open)\b", re.I)
_REL_WORDS = re.compile(r"\b(girlfriend|boyfriend|partner|crush|dating)\b", re.I)
_VERB = {
    "works_at": "works at", "lives_in": "lives in", "reports_to": "reports to", "knows": "knows",
    "works_with": "works with", "met_at": "met at", "talked_about": "talked about", "decided": "decided",
    "promised": "promised", "owed_by": "is owed to", "blocked_by": "is blocked by", "part_of": "is part of",
    "about": "is about", "attended": "attended", "likes": "likes", "dislikes": "dislikes", "prefers": "prefers",
    "uses": "uses", "owns": "owns", "follows": "follows", "collaborates_with": "collaborates with",
    "deadline_of": "is the deadline of", "contributes_to": "contributes to", "supersedes": "supersedes",
    "works_on": "works on", "with": "is with",
}
_PAST = {"works_at": "worked at", "lives_in": "lived in", "reports_to": "reported to"}


def _day(ts: str | None) -> str | None:
    return ts[:10] if ts else None


def _cite(ug: UserGraph, target_uid: str, edge: bool) -> dict | None:
    rows = ug.ro(Q.WHY_EDGE if edge else Q.WHY_NODE, {"uid": target_uid})
    if not rows:
        return None
    r = rows[0]
    return {"source_id": r["source_id"], "source_type": r["source_type"], "title": r["title"], "when": _day(r["ts"]),
            "quote": r["quote"]}


def _anchors(ug: UserGraph, question: str, people_hint: list[str]) -> tuple[list[dict], list[str] | None]:
    """Entities the question names. A bare first name that fits several people -> which_person."""
    text = " " + " ".join(M.words(question + " " + " ".join(people_hint))) + " "
    found: dict[str, dict] = {}
    first_names: dict[str, list[dict]] = {}
    ents = ug.ro(Q.ENTITIES_FOR_LOOKUP)
    for e in ents:
        for nm in e["names_norm"] or []:
            if len(nm) >= 3 and f" {' '.join(M.words(nm))} " in text:
                found[e["uid"]] = e
        if e["type"] == "person":
            first = M.words(e["name"])[0] if M.words(e["name"]) else ""
            if len(first) >= 3 and f" {first} " in text:
                first_names.setdefault(first, []).append(e)
    which = None
    for first, cands in first_names.items():
        full = [c for c in cands if c["uid"] in found and " " in M.norm(c["name"])]
        if full:
            continue  # a full name was said ("Rohan Mehta"): no doubt
        if len(cands) == 1:
            found[cands[0]["uid"]] = cands[0]
        else:
            which = sorted(c["name"] for c in cands)[:5]  # "Rohan" fits more than one person: the AI should ask
    # "Rohan Mehta" was said: the bare "Rohan" node is not what the question is about
    fulls = [M.norm(e["name"]) for e in found.values() if " " in M.norm(e["name"])]
    for k in [k for k, e in found.items() if e["type"] == "person" and " " not in M.norm(e["name"])]:
        if any(M.is_first_name_of(found[k]["name"], f) for f in fulls):
            del found[k]
    # specific names first (people), then the rest
    out = sorted(found.values(), key=lambda e: (e["type"] != "person", e["name"]))
    return out[:5], which


def get_context(ug: UserGraph, question: str, allowed_categories: list[str] | None = None, tier_max: int = 1,
                people_hint: list[str] | None = None) -> dict:
    """A small, cited slice of the user's graph for one question, inside the caller's categories and tier."""
    allowed = set(allowed_categories if allowed_categories is not None else M.DEFAULT_ALLOWED)
    allowed &= set(M.ALL_CATEGORIES)
    if "relationships" not in allowed:
        tier_max = min(tier_max, 1)
    tier_max = max(0, min(2, int(tier_max)))
    left_out = sorted(set(M.ALL_CATEGORIES) - allowed)
    filtered = {"n": 0}

    def ok(cats, sens) -> bool:
        keep = set(cats or []) <= allowed and int(sens or 0) <= tier_max
        if not keep:
            filtered["n"] += 1
        return keep

    if not allowed & {"about_me", "people", "plans", "calendar", "conversations"}:
        return {"question": question, "off": True, "sections": [], "text": "Fally memory is off for this connection.",
                "left_out": left_out}
    if _REL_WORDS.search(question) and "relationships" not in allowed:
        note = ("Questions about close relationships need the Close relationships switch in Fally for this "
                "connection.")
        return {"question": question, "sections": [], "text": note, "left_out": left_out, "resolved": {"people": []}}

    anchors, which = _anchors(ug, question, people_hint or [])
    sections: dict[str, list[dict]] = {"Who": [], "What's true now": [], "What changed": [], "Open": [],
                                       "Evidence": [], "Around it": []}
    resolved = []
    me = (ug.ro(Q.GET_SELF) or [{"uid": None}])[0]["uid"]
    seen_edges: set[str] = set()
    for a in anchors:
        if not ok(a["categories"], a["sens"]):
            continue
        node = ug.ro(Q.GET_NODE, {"uid": a["uid"]})[0]
        props = json.loads(node["props"] or "{}")
        bits = [node["name"], f"({node['type']})"]
        if node["aliases"]:
            bits.append("aka " + ", ".join(node["aliases"][:3]))
        if props.get("label"):
            lab_sens = M.text_sens(props["label"])
            if lab_sens <= tier_max and (lab_sens < 2 or "relationships" in allowed):
                bits.append(f"- {props['label']}")
        resolved.append({"uid": node["uid"], "name": node["name"], "type": node["type"]})
        sections["Who"].append({"id": f"node:{node['uid']}", "kind": "entity", "text": " ".join(bits),
                                "when": _day(node["last_seen"]), "source": _cite(ug, node["uid"], False),
                                "categories": node["categories"], "sens": node["sens"]})
        facts = ug.ro(Q.FACTS_OF_NODE.replace("{ENTITY_RELS}", M.ENTITY_RELS), {"uid": node["uid"]})
        for f in facts:
            if f["uid"] in seen_edges or not ok(f["categories"], f["sens"]):
                continue
            seen_edges.add(f["uid"])
            t = f["rel"].lower()
            src_name = "You" if f["src"] == me else f["src_name"]
            dst_name = "you" if f["dst"] == me else f["dst_name"]
            if f["valid_to"]:
                text = f"{src_name} {_PAST.get(t, _VERB.get(t, t))} {dst_name} (until {_day(f['valid_to'])})"
                sec = "What changed"
            else:
                since = f" (since {_day(f['valid_from'])})" if f["valid_from"] else ""
                verb = _VERB.get(t, t)
                if src_name == "You":  # "You work with", not "You works with"
                    first, _, rest = verb.partition(" ")
                    first = {"is": "are"}.get(first, first[:-1] if first.endswith("s") else first)
                    verb = f"{first} {rest}".strip()
                text = f"{src_name} {verb} {dst_name}{since}"
                sec = "What's true now"
            if t in ("promised", "owed_by"):
                continue  # promises are listed under Open with both sides
            sections[sec].append({"id": f"edge:{f['uid']}", "kind": "fact", "text": text,
                                  "confidence": round(float(f["confidence"] or 0), 2), "basis": f["basis"],
                                  "weight": f["weight"], "source": _cite(ug, f["uid"], True),
                                  "categories": f["categories"], "sens": f["sens"]})
        if node["type"] == "person":
            for o in open_between(ug, node["uid"]):
                if not ok(o["categories"], o["sens"]):
                    continue
                who = "You owe " + o["person"] if o["direction"] == "you_owe" else o["person"] + " owes you"
                due = f" (due {_day(o['due'])})" if o["due"] else ""
                sections["Open"].append({"id": f"promise:{o['commitment']}", "kind": "promise",
                                         "text": f"{who}: {o['text']}{due}", "source": _cite(ug, o["edge"], True),
                                         "categories": o["categories"], "sens": o["sens"]})
            nb = neighbourhood(ug, node["uid"], depth=2, limit=60)
            names = sorted({n["name"] for n in nb["nodes"]
                            if n["uid"] != node["uid"] and n["type"] not in ("self", "commitment")
                            and set(n["categories"] or []) <= allowed and int(n["sens"] or 0) <= tier_max})
            if names:
                sections["Around it"].append({"id": f"around:{node['uid']}", "kind": "neighbourhood",
                                              "text": f"Near {node['name']}: " + ", ".join(names[:10]),
                                              "categories": ["people"], "sens": 0})
    if _PLANS.search(question) and not any(sections["Open"]):
        for o in ug.ro(Q.ALL_OPEN_PROMISES):
            if not ok(o["categories"], o["sens"]):
                continue
            who = "You owe " + o["person"] if o["direction"] == "you_owe" else o["person"] + " owes you"
            due = f" (due {_day(o['due'])})" if o["due"] else ""
            sections["Open"].append({"id": f"promise:{o['commitment']}", "kind": "promise",
                                     "text": f"{who}: {o['text']}{due}", "source": _cite(ug, o["edge"], True),
                                     "categories": o["categories"], "sens": o["sens"]})
    # Evidence: quotes that match the question words (FalkorDB full-text index over Mention.quote)
    terms = [w for w in M.words(question) if len(w) >= 4 and w not in _STOP]
    if terms:
        try:
            hits = ug.ro(Q.SEARCH_QUOTES, {"q": "|".join(terms[:8]), "limit": 12})
        except Exception:
            hits = []
        cited = {(i.get("source") or {}).get("quote") for sec in sections.values() for i in sec}
        for h in hits:
            if h["quote"] in cited:
                continue
            cats = sorted(set(h["categories"] or []) | ({"people"} if h["target_kind"] else set()))
            if h["target_uid"] in seen_edges:
                continue
            if not ok(cats, h["sens"]):
                continue
            sections["Evidence"].append({"id": f"quote:{h['mention']}", "kind": "quote", "text": f"\"{h['quote']}\"",
                                         "source": {"source_id": h["source_id"], "source_type": h["source_type"],
                                                    "title": h["title"], "when": _day(h["ts"])},
                                         "categories": cats, "sens": h["sens"]})
            if len(sections["Evidence"]) >= 4:
                break
    out_sections = [{"title": k, "tier": "core" if k in ("Who", "What's true now", "Open") else "broad", "items": v}
                    for k, v in sections.items() if v]
    return {"question": question, "resolved": {"people": resolved}, "which_person": which,
            "allowed_categories": sorted(allowed), "tier_max": tier_max, "sections": out_sections,
            "left_out": left_out, "filtered_out": filtered["n"], "text": _markdown(out_sections, left_out, which)}


_STOP = {"what", "when", "where", "which", "about", "with", "have", "does", "that", "this", "from", "there", "their",
         "they", "them", "your", "should", "would", "could", "know", "tell", "between", "anything", "last"}


def _markdown(sections: list[dict], left_out: list[str], which: list[str] | None) -> str:
    lines: list[str] = []
    if which:
        lines.append(f"Which one? The name fits: {', '.join(which)}.")
    for s in sections:
        lines.append(f"## {s['title']}")
        for it in s["items"]:
            src = it.get("source") or {}
            cite = f" [{src.get('title') or src.get('source_id')}, {src.get('when')}]" if src else ""
            quote = f" - \"{src['quote']}\"" if src.get("quote") and it["kind"] != "quote" else ""
            lines.append(f"- {it['text']}{cite}{quote}")
    if not sections:
        lines.append("Nothing this connection may see matches the question.")
    if left_out:
        lines.append("## Left out")
        lines.append("Not shared with this connection: " + ", ".join(left_out))
    return "\n".join(lines)
