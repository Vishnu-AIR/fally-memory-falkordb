"""FastAPI app: the memory graph over HTTP, a tiny MCP-style endpoint, and the demo page."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import model as M, reader, writer
from .db import UserGraph, list_users

app = FastAPI(title="Fally memory graph on FalkorDB", version="0.1.0")
STATIC = Path(__file__).parent / "static"
DEMO_USERS_ONLY = os.environ.get("DEMO_USERS_ONLY", "0") == "1"


def _ug(user_id: str) -> UserGraph:
    try:
        ug = UserGraph(user_id)
    except ValueError:
        raise HTTPException(400, "bad user id")
    return ug


def _existing(user_id: str) -> UserGraph:
    ug = _ug(user_id)
    if not ug.exists():
        raise HTTPException(404, "no such user")
    return ug


@app.exception_handler(writer.GraphError)
def _graph_error(_: Request, e: writer.GraphError):
    return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    t = time.perf_counter()
    users = list_users()
    return {"ok": True, "users": len(users), "ms": round((time.perf_counter() - t) * 1000, 1),
            "graph_db": _falkor_info(), "host": os.environ.get("DEPLOY_HOST", "local")}


def _falkor_info() -> dict[str, Any]:
    """Which graph database answers this API: FalkorDB version and how many per-user graphs it holds."""
    from .db import db
    info: dict[str, Any] = {"engine": "FalkorDB"}
    try:
        conn = db().connection
        for mod in conn.execute_command("MODULE", "LIST"):
            m = {mod[i].decode() if isinstance(mod[i], bytes) else mod[i]: mod[i + 1] for i in range(0, len(mod) - 1, 2)}
            name = m.get("name")
            name = name.decode() if isinstance(name, bytes) else name
            if name == "graph":
                v = int(m.get("ver", 0))
                info["version"] = f"{v // 10000}.{v // 100 % 100}.{v % 100}"
        info["graphs"] = len(db().list_graphs())
    except Exception as e:  # health must never fail because of this
        info["error"] = type(e).__name__
    return info


@app.get("/api/users")
def users():
    from synth.load import USERS
    out = []
    for uid in list_users():
        meta = USERS.get(uid, ({"id": uid, "name": uid, "about": ""}, []))[0]
        out.append({"id": uid, "name": meta["name"], "about": meta.get("about", ""), "graph": M.graph_name(uid)})
    return out


class IngestBody(BaseModel):
    source: dict
    graph: dict


@app.post("/api/users/{user_id}/ingest")
def ingest(user_id: str, body: IngestBody):
    if DEMO_USERS_ONLY:
        from synth.load import USERS
        if user_id not in USERS:
            raise HTTPException(403, "the live demo only writes to the synthetic users")
    ug = _ug(user_id)
    t = time.perf_counter()
    res = writer.ingest(ug, body.model_dump())
    return {"nodes": res.nodes, "edges": res.edges, "closed": res.closed,
            "ms": round((time.perf_counter() - t) * 1000, 1)}


@app.get("/api/users/{user_id}/people")
def people(user_id: str):
    return reader.people(_existing(user_id))


@app.get("/api/users/{user_id}/graph")
def graph(user_id: str):
    return reader.overview(_existing(user_id))


@app.get("/api/users/{user_id}/nodes/{uid}/neighbourhood")
def neighbourhood(user_id: str, uid: str, depth: int = 2):
    return reader.neighbourhood(_existing(user_id), uid, depth)


@app.get("/api/users/{user_id}/path")
def path(user_id: str, a: str, b: str, max_hops: int = 4):
    p = reader.path(_existing(user_id), a, b, max_hops)
    return p or {"hops": None, "nodes": [], "edges": []}


@app.get("/api/users/{user_id}/why/{uid}")
def why(user_id: str, uid: str):
    return reader.why(_existing(user_id), uid)


@app.get("/api/users/{user_id}/open-between/{person_uid}")
def open_between(user_id: str, person_uid: str):
    return reader.open_between(_existing(user_id), person_uid)


class ContextBody(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    allowed_categories: list[str] | None = None
    tier_max: int = 1
    people: list[str] = Field(default_factory=list, max_length=5)


@app.post("/api/users/{user_id}/context")
def context(user_id: str, body: ContextBody):
    t = time.perf_counter()
    out = reader.get_context(_existing(user_id), body.question, body.allowed_categories, body.tier_max, body.people)
    out["ms"] = round((time.perf_counter() - t) * 1000, 1)
    return out


class MergeBody(BaseModel):
    from_uid: str
    into_uid: str


@app.post("/api/users/{user_id}/merge")
def merge(user_id: str, body: MergeBody):
    return {"merge": writer.merge(_existing(user_id), body.from_uid, body.into_uid)}


@app.post("/api/users/{user_id}/merges/{merge_uid}/undo")
def undo(user_id: str, merge_uid: str):
    return writer.undo_merge(_existing(user_id), merge_uid)


@app.delete("/api/users/{user_id}/people/{person_uid}")
def forget(user_id: str, person_uid: str):
    return writer.forget_person(_existing(user_id), person_uid)


@app.post("/api/users/{user_id}/reset")
def reset(user_id: str):
    from synth.load import USERS, load_user
    if user_id not in USERS:
        raise HTTPException(404, "only synthetic users can be reset")
    return load_user(user_id)


# ---------------------------------------------------------------- MCP-style tool (JSON-RPC over HTTP)
GET_CONTEXT_TOOL = {
    "name": "get_context",
    "description": "A small, cited slice of the user's private memory graph for one question, filtered by the "
                   "connection's categories and sensitivity tier.",
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["question"],
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 500},
            "people": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        },
    },
}


@app.post("/mcp/{user_id}")
def mcp(user_id: str, msg: dict = Body(...), categories: str = "about_me,people,plans,calendar,conversations"):
    """Demo MCP endpoint. In Fally the user and the categories come from the OAuth grant; here from the URL."""
    rid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        result: Any = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fally-memory-falkordb", "version": "0.1"},
                       "capabilities": {"tools": {}}}
    elif method == "tools/list":
        result = {"tools": [GET_CONTEXT_TOOL]}
    elif method == "tools/call" and (msg.get("params") or {}).get("name") == "get_context":
        args = (msg.get("params") or {}).get("arguments") or {}
        allowed = [c for c in categories.split(",") if c]
        out = reader.get_context(_existing(user_id), str(args.get("question", ""))[:500], allowed,
                                 2 if "relationships" in allowed else 1, args.get("people") or [])
        result = {"content": [{"type": "text", "text": out["text"]}], "structuredContent": out}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}
