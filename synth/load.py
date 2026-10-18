"""Load the synthetic users into FalkorDB: one graph per user, dropped and rebuilt."""

from __future__ import annotations

from fally_graph import writer
from fally_graph.db import UserGraph

from .generate import generate

USERS = {u["id"]: (u, blocks) for u, blocks in generate()}


def load_user(user_id: str) -> dict:
    user, blocks = USERS[user_id]
    ug = UserGraph(user_id)
    ug.drop()
    ug = UserGraph(user_id)
    writer.ensure_self(ug, user["name"])
    for b in blocks:
        writer.ingest(ug, b)
    return {"user": user_id, "blocks": len(blocks)}


def load_all() -> list[dict]:
    return [load_user(u) for u in USERS]
