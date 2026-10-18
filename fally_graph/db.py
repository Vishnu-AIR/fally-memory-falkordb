"""FalkorDB connection and the one-graph-per-user rule.

Multi-tenancy: every user gets their own FalkorDB graph (`db.select_graph("fally_user_<id>")`). A FalkorDB graph is
a separate key in the server with its own nodes, edges and indexes, so a query on one user's graph cannot see or
touch another user's data, and deleting a user is `graph.delete()`.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
from typing import Any

from falkordb import FalkorDB

from . import model, queries as Q

_db: FalkorDB | None = None
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_ready: set[str] = set()

# Per-request log of the Cypher sent to FalkorDB, so the API and demo page can show exactly what ran.
_trace: contextvars.ContextVar[list | None] = contextvars.ContextVar("falkordb_trace", default=None)


def trace_start() -> list:
    t: list = []
    _trace.set(t)
    return t


def _log(graph: str, cypher: str, res, t0: float, mode: str) -> None:
    t = _trace.get()
    if t is None or len(t) >= 60:
        return
    server_ms = getattr(res, "run_time_ms", None)
    t.append({"graph": graph, "mode": mode, "cypher": " ".join(cypher.split())[:400],
              "rows": len(res.result_set or []),
              "server_ms": round(float(server_ms), 3) if server_ms is not None else None,
              "ms": round((time.perf_counter() - t0) * 1000, 2)})


def db() -> FalkorDB:
    global _db
    if _db is None:
        _db = FalkorDB(host=os.environ.get("FALKORDB_HOST", "localhost"),
                       port=int(os.environ.get("FALKORDB_PORT", "6379")))
    return _db


def sub(template: str, **subs: Any) -> str:
    """Fill {REL}/{LABEL}/... placeholders (whitelisted values only; see model.py)."""
    out = template.replace("{ENTITY_RELS}", model.ENTITY_RELS)
    for k, v in subs.items():
        out = out.replace("{" + k + "}", str(v))
    return out


class UserGraph:
    """One user's graph. All reads and writes for that user go through here."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.name = model.graph_name(user_id)
        self.g = db().select_graph(self.name)
        with _locks_guard:
            self.lock = _locks.setdefault(self.name, threading.RLock())

    def q(self, cypher: str, params: dict | None = None) -> list[dict]:
        t0 = time.perf_counter()
        res = self.g.query(cypher, params or {})
        _log(self.name, cypher, res, t0, "query")
        header = [h[1] if isinstance(h, (list, tuple)) else h for h in (res.header or [])]
        return [dict(zip(header, row)) for row in res.result_set]

    def ro(self, cypher: str, params: dict | None = None) -> list[dict]:
        t0 = time.perf_counter()
        res = self.g.ro_query(cypher, params or {})
        _log(self.name, cypher, res, t0, "ro_query")
        header = [h[1] if isinstance(h, (list, tuple)) else h for h in (res.header or [])]
        return [dict(zip(header, row)) for row in res.result_set]

    def one(self, cypher: str, params: dict | None = None) -> dict | None:
        rows = self.q(cypher, params)
        return rows[0] if rows else None

    def ensure_schema(self) -> None:
        if self.name in _ready:
            return
        for stmt in Q.SCHEMA:
            try:
                self.g.query(stmt)
            except Exception as e:  # index already exists
                if "already" not in str(e).lower() and "exists" not in str(e).lower():
                    raise
        _ready.add(self.name)

    def exists(self) -> bool:
        return self.name in db().list_graphs()

    def drop(self) -> None:
        if self.exists():
            self.g.delete()
        _ready.discard(self.name)


def list_users() -> list[str]:
    prefix = "fally_user_"
    return sorted(n[len(prefix):] for n in db().list_graphs() if n.startswith(prefix))
