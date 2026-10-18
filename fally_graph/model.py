"""The memory graph model: node types, relationship types, write rules and privacy words.

Every constant here is checked before it reaches Cypher, so relationship types that have to be spliced into a
query string (Cypher has no parameter for a relationship type) only ever come from this whitelist.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

# 16 node types. Each node carries the label :Entity plus one type label (Person, Organisation, ...).
NODE_TYPES = {
    "self": "Self",
    "person": "Person",
    "organisation": "Organisation",
    "place": "Place",
    "topic": "Topic",
    "project": "Project",
    "goal": "Goal",
    "commitment": "Commitment",
    "decision": "Decision",
    "event": "Event",
    "document": "Document",
    "product": "Product",
    "account": "Account",
    "device": "Device",
    "artifact": "Artifact",
    "routine": "Routine",
}

# Relationship types between entities (stored as upper-case Cypher relationship types, e.g. WORKS_AT).
EDGE_TYPES = {
    "knows", "works_at", "works_with", "reports_to", "lives_in", "met_at", "talked_about", "decided",
    "promised", "owed_by", "blocked_by", "part_of", "about", "attended", "likes", "dislikes", "prefers",
    "uses", "owns", "follows", "collaborates_with", "deadline_of", "contributes_to", "supersedes",
    "works_on", "with",
}

# Functional relationships: one current destination per (src, type). A new destination closes the old period.
FUNCTIONAL_TYPES = {"works_at", "lives_in", "reports_to"}

BASIS_RANK = {"system": 0, "inferred": 1, "enriched": 2, "connector": 3, "stated": 4, "user": 5}
SOURCE_TYPES = {"meeting", "conversation", "whatsapp", "mail", "calendar", "user", "seed"}

# Which privacy category a source needs before anything it said may leave the app (contract: "categories").
SOURCE_CATEGORIES = {
    "meeting": ["conversations"],
    "conversation": ["conversations"],
    "whatsapp": ["whatsapp"],
    "mail": ["mail"],
    "calendar": ["calendar"],
    "user": [],
    "seed": [],
}
ALL_CATEGORIES = ["about_me", "people", "plans", "calendar", "conversations", "mail", "whatsapp", "relationships"]
DEFAULT_ALLOWED = ["about_me", "people", "plans", "calendar", "conversations"]
PLAN_EDGES = {"promised", "owed_by", "deadline_of"}
KEY_ONLY_TYPES = {"commitment", "decision"}  # same words, different promise: identified by keys only

# Sensitivity: 0 ordinary, 1 personal (family), 2 intimate. sens 2 also needs the `relationships` category.
_INTIMATE = re.compile(
    r"\b(girlfriend|boyfriend|dating|date night|crush|romantic|breakup|broke up|kissed|in love|my ex)\b", re.I
)
_PERSONAL = re.compile(r"\b(mom|mum|dad|sister|brother|grandma|grandpa|therapist|therapy)\b", re.I)

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9']+")


def rel(edge_type: str) -> str:
    """The Cypher relationship type for an edge type, only from the whitelist."""
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"unknown edge type {edge_type!r}")
    return edge_type.upper()


def label(node_type: str) -> str:
    if node_type not in NODE_TYPES:
        raise ValueError(f"unknown node type {node_type!r}")
    return NODE_TYPES[node_type]


ENTITY_RELS = "|".join(sorted(rel(t) for t in EDGE_TYPES))  # for variable-length patterns: entities only


def norm(text: Any) -> str:
    return _WS.sub(" ", str(text or "").strip().lower())


def words(text: Any) -> list[str]:
    return _WORD.findall(norm(text))


def text_sens(*texts: Any) -> int:
    joined = " ".join(str(t) for t in texts if t)
    if _INTIMATE.search(joined):
        return 2
    if _PERSONAL.search(joined):
        return 1
    return 0


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts(value: Any) -> str | None:
    """LLM-written dates never break a query: anything unparseable becomes None. Stored as sortable ISO strings."""
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone(timezone.utc)
        return d.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def strs(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if isinstance(v, (str, int)) and str(v).strip()]


def confidence(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def union(*lists: Iterable[str]) -> list[str]:
    out: set[str] = set()
    for item in lists:
        out.update(item or [])
    return sorted(out)


def is_first_name_of(short: str, full: str) -> bool:
    s, f = norm(short), norm(full)
    return bool(s) and " " not in s and " " in f and f.split(" ")[0] == s


def clean_aliases(node_type: str, name: str, aliases: Iterable[str]) -> list[str]:
    """Never the node's own name, never a voice label, and for a person with a full name never the bare first name
    (every Asha is "Asha": a first name is a lookup hint, not an identity)."""
    out, seen = [], {norm(name)}
    for a in strs(list(aliases or [])):
        k = norm(a)
        if not k or k in seen or re.search(r"\bspeaker\s*\d+\b", k):
            continue
        if node_type == "person" and is_first_name_of(k, name):
            continue
        seen.add(k)
        out.append(a)
    return out


def name_match(name: str, aliases: Iterable[str], other_name: str, other_aliases: Iterable[str]) -> str | None:
    """'strong' (same node), 'weak' (only a "same as?" candidate) or None. A shared single word ("Rohan" and
    "Rohan") is never enough to call two people the same person."""
    n, on = norm(name), norm(other_name)
    mine = {n, *(norm(a) for a in aliases or [])} - {""}
    theirs = {on, *(norm(a) for a in other_aliases or [])} - {""}
    shared = mine & theirs
    if not n:
        return None
    if not shared:
        if is_first_name_of(n, on) or is_first_name_of(on, n):
            return "weak"
        return None
    if not any(" " in x for x in shared):
        return "weak"
    if n == on:
        return "strong"
    if n in theirs and " " in n:
        return "strong"
    if on in mine and " " in on and " " not in n:
        return "strong"
    return "weak"


def edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def graph_name(user_id: str) -> str:
    """One FalkorDB graph per user. The id is restricted so it can never address another graph."""
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", user_id or ""):
        raise ValueError("bad user id")
    return f"fally_user_{user_id}"
