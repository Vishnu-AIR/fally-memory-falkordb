# Fally memory graph on FalkorDB

**Live demo:** https://falkor.fally.online (synthetic users only)
**Demo video:** YouTube: TBA
**Built for:** WeMakeDevs Graph Hacks (FalkorDB), during the event (Oct 15 to 18, 2026).

Fally is a private, memory-first assistant: it listens to your meetings, chats, mail and calendar (with your
permission) and keeps a personal knowledge graph of the people, organisations, places, projects and promises in
your life. This repo is **the memory graph part only**, rebuilt with **FalkorDB as the primary graph database**:

- an extractor-style **ingest** (`{nodes, edges}` blocks with quotes) with strong-evidence **entity resolution** and
  "same as?" candidates,
- **time-aware facts**: functional relationships (`WORKS_AT`, `LIVES_IN`, `REPORTS_TO`) close the old period when a
  new one starts, so the graph knows both "works at Northwind now" and "worked at Lumen until Oct 1",
- **provenance** on every node and edge (`Mention` nodes back to the exact quote and `Source`),
- **merge + undo** for duplicate people, and **forget a person** (node, edges, promises and quotes),
- graph reads: **neighbourhood** (depth 2, never walking through the user's own `Self` hub), **bounded path**,
  **"what's open between me and X"** (promises both ways), **why is this true?**,
- **`get_context`**: the small, cited slice an outside AI (Claude, ChatGPT via MCP) gets for one question,
  filtered by the categories that connection may see and by sensitivity (`sens`).

All data in this repo and in the live demo is **synthetic** (`synth/generate.py`). No real people.

## Multi-tenancy: one FalkorDB graph per user

Every user is their own FalkorDB graph: `db.select_graph("fally_user_<id>")` (see `fally_graph/db.py`).
A FalkorDB server holds many named graphs (`GRAPH.LIST` / `db.list_graphs()`); each is a separate key with its own
nodes, edges and indexes, and every `GRAPH.QUERY` names exactly one graph. So no query needs a `user_id` filter, a
query for one user cannot see another user's data even if it has a bug, and deleting a user is `graph.delete()`
(`GRAPH.DELETE`). User ids are validated (`[a-z0-9_-]{1,64}`) before they become a graph name. The isolation test
(`tests/test_graph.py::test_one_graph_per_user_and_no_leaks`) checks that the same name in two users gives two
different nodes, that one user's node id is unknown in the other's graph, and that dropping one graph leaves the
other intact.

## Where the Cypher lives

**All Cypher is in [`fally_graph/queries.py`](fally_graph/queries.py)**, one named constant per query (schema,
resolution, edge rule, provenance, merge, neighbourhood, path, open-between, get_context reads, forget).
The Python around it:

| File | What |
|---|---|
| `fally_graph/queries.py` | every Cypher query |
| `fally_graph/model.py` | node types, relationship types, functional types, categories, `sens` words, name rules |
| `fally_graph/writer.py` | ingest, resolution, the edge rule, merge / undo, forget |
| `fally_graph/reader.py` | people, neighbourhood, path, why, open-between, `get_context` |
| `fally_graph/db.py` | FalkorDB connection, one graph per user |
| `fally_graph/api.py` | FastAPI endpoints + MCP-style `get_context` tool |
| `fally_graph/static/index.html` | the demo page |
| `synth/generate.py` | synthetic users, meetings, chats, mails, calendar events, promises |
| `tests/test_graph.py` | tests against a real FalkorDB |

Cypher has no parameters for labels or relationship types, so the few queries that need one (`{REL}`, `{LABEL}`,
`{ENTITY_RELS}`) are filled only from the whitelists in `model.py`. Everything else is a `$parameter`.

## Graph data model

### Node labels

Every entity has the label `:Entity` plus one type label.

| Label | Meaning |
|---|---|
| `:Entity:Self` | the user (exactly one per graph) |
| `:Entity:Person` | a person |
| `:Entity:Organisation`, `:Place`, `:Topic`, `:Project`, `:Goal`, `:Event`, `:Document`, `:Product`, `:Account`, `:Device`, `:Artifact`, `:Routine` | other entities |
| `:Entity:Commitment` | a promise (props `due`, `status`) |
| `:Entity:Decision` | a decision |
| `:Mention` | one quote that supports a node or an edge |
| `:Source` | where the quote came from (a meeting, chat, mail, calendar event) |
| `:Merge` | an undoable merge, with its snapshot |

Entity properties:

| Property | Type | Notes |
|---|---|---|
| `uid` | string | stable id (indexed) |
| `type` | string | one of the 16 node types (indexed) |
| `name`, `aliases`, `names_norm` | string, string[], string[] | `names_norm` = normalised name + aliases, for exact matching |
| `keys` | string[] | strong ids: `email:<hash>`, `phone:<hash>`, `commitment:<episode>:<n>` |
| `search_text` | string | name + aliases, full-text indexed (fuzzy "same as?" search) |
| `same_as` | string[] | "same as?" candidates (uids) for the People screen |
| `merged_into` | string | set when merged; readers skip merged nodes |
| `first_seen`, `last_seen`, `importance` | string (ISO UTC), string, float | |
| `categories`, `sens` | string[], int | privacy: categories its sources need, sensitivity 0/1/2 |
| `props` | string (JSON) | free-form extractor props (FalkorDB properties cannot be maps) |
| `due`, `status`, `label` | string | promoted from `props` so Cypher can filter on them |

### Relationship types

Between entities: `KNOWS`, `WORKS_AT`, `WORKS_WITH`, `REPORTS_TO`, `LIVES_IN`, `MET_AT`, `TALKED_ABOUT`, `DECIDED`,
`PROMISED`, `OWED_BY`, `BLOCKED_BY`, `PART_OF`, `ABOUT`, `ATTENDED`, `LIKES`, `DISLIKES`, `PREFERS`, `USES`, `OWNS`,
`FOLLOWS`, `COLLABORATES_WITH`, `DEADLINE_OF`, `CONTRIBUTES_TO`, `SUPERSEDES`, `WORKS_ON`, `WITH`.
Provenance: `(:Entity)-[:MENTIONED_IN]->(:Mention)-[:FROM]->(:Source)`.

A promise is `(owner)-[:PROMISED]->(:Commitment)-[:OWED_BY]->(recipient)`.

| Edge property | Notes |
|---|---|
| `uid` | stable id |
| `confidence` | 0..1, the max seen |
| `basis` | `stated`, `inferred`, `enriched`, `connector`, `system`, `user` (the stronger wins) |
| `weight` | grows by 1 per distinct source (`sources` holds the source ids, so re-ingesting is idempotent) |
| `valid_from`, `valid_to` | validity period (ISO UTC strings); `valid_to` null = true now |
| `contested` | a weaker claim against a stronger current functional edge (kept as evidence, never current) |
| `categories`, `sens` | privacy, as on nodes |

Edge quotes are `:Mention {target_kind: 'edge', target_uid: <edge uid>}` nodes (a relationship cannot have
relationships), indexed on `target_uid`.

### Indexes

Range indexes on `Entity.uid`, `Entity.type`, `Mention.uid`, `Mention.target_uid`, `Source.source_id`, `Merge.uid`;
full-text indexes on `Mention.quote` (question words to quotes) and `Entity.search_text` (fuzzy names).

## The write rules

- **Resolution needs strong evidence**: a shared key, an exact multi-word name or alias, or (non-person types) the
  exact normalised name. A shared first name is never enough: "Rohan" from WhatsApp and "Rohan Mehta" from the
  calendar stay two nodes, and the new one carries the other as a `same_as` candidate (found with the fuzzy
  full-text index). Commitments and decisions with keys resolve by keys only (same words, different promise).
- **Functional edges** (`WORKS_AT`, `LIVES_IN`, `REPORTS_TO`): one current destination per source. A new
  destination closes the weaker current period at the new start; a stronger current edge makes the claim
  `contested`; a claim older than the current period is stored as a past period; A then B then A again restarts A as
  a new period and keeps the old one as history.
- **Provenance**: every quote becomes a `Mention` linked to its `Source`.
- **Merge** folds duplicate edges (weights and sources add), drops edges between the two nodes, moves quotes, adds
  the old name as an alias, and stores a snapshot; **undo** restores edges, quotes and aliases exactly.
- **Forget a person** deletes the node, every edge touching it, promises only they were part of, every quote about
  any of these, and sources left with no quotes.

## Privacy in `get_context`

Each item carries `categories` (every category its sources need: `conversations` for meetings, `whatsapp`, `mail`,
`calendar`, plus `people`, `plans`, `about_me`) and `sens` (0 ordinary, 1 personal, 2 intimate; intimate words like
"girlfriend" or "date night" give 2 and add `relationships`). A connection keeps an item only when
`categories` is a subset of its allowed categories and `sens <= tier_max` (`tier_max` is capped at 1 unless
`relationships` is allowed). Filtered items are counted but never hinted at; a close-relationship question without
the switch gets a fixed note and no names. Every item cites its source and quote.

## API

| Method | Path | What |
|---|---|---|
| GET | `/api/users` | synthetic users (one graph each) |
| POST | `/api/users/{user}/ingest` | ingest one extractor block `{source, graph: {nodes, edges}}` |
| GET | `/api/users/{user}/people` | people with current employer and "same as?" candidates |
| GET | `/api/users/{user}/graph` | current edges, for the demo graph |
| GET | `/api/users/{user}/nodes/{uid}/neighbourhood?depth=2` | depth 1 or 2, never through Self |
| GET | `/api/users/{user}/path?a=&b=&max_hops=4` | bounded shortest path, never through Self |
| GET | `/api/users/{user}/why/{uid}` | provenance of an edge or node: quotes and sources |
| GET | `/api/users/{user}/open-between/{person}` | promises you owe them and they owe you |
| POST | `/api/users/{user}/context` | `get_context`: `{question, allowed_categories, tier_max}` |
| POST | `/api/users/{user}/merge` | `{from_uid, into_uid}` returns a merge id |
| POST | `/api/users/{user}/merges/{merge}/undo` | undo a merge |
| DELETE | `/api/users/{user}/people/{person}` | forget a person |
| POST | `/api/users/{user}/reset` | reload a synthetic user |
| POST | `/mcp/{user}` | MCP-style JSON-RPC: `tools/list`, `tools/call get_context` |

Example (live):

```bash
curl -s -X POST https://falkor.fally.online/api/users/asha/context \
  -H 'content-type: application/json' \
  -d '{"question": "What is open between me and Tomas?"}'
```

The `text` field (what an AI receives), about 20 ms:

```
## Who
- Tomas Reyes (person) [Coffee with Tomas, 2026-10-07] - "Tomas: I'm joining Northwind Bikes next month."
## What's true now
- You work with Tomas Reyes [Atlas pricing review, 2026-09-16] - "Tomas and I sync every Tuesday on Atlas"
- Tomas Reyes works at Northwind Bikes (since 2026-10-01) [Coffee with Tomas, 2026-10-07] - "Tomas: I start at Northwind Bikes on the 1st."
## What changed
- Tomas Reyes worked at Lumen Labs (until 2026-10-01) [Atlas pricing review, 2026-09-16] - "Tomas, who runs pricing at Lumen Labs"
## Open
- You owe Tomas Reyes: Send Tomas the Atlas pricing deck (due 2026-10-20) [Atlas pricing review, 2026-09-16] - "Me: I'll send you the pricing deck before the 20th."
- Tomas Reyes owes you: Tomas to introduce Asha to Kofi Mensah (due 2026-10-22) [Coffee with Tomas, 2026-10-07] - "Tomas: I'll intro you to Kofi this week, promise."
## Evidence
- "Tomas: you should meet Kofi Mensah, he built Northwind's app" [Coffee with Tomas, 2026-10-07]
- "Tomas: I'll own the pricing model for Atlas." [Atlas pricing review, 2026-09-16]
## Around it
- Near Tomas Reyes: Kofi Mensah, Northwind Bikes
## Left out
Not shared with this connection: mail, relationships, whatsapp
```

and `sections[].items[]` carry `id`, `text`, `source {source_id, source_type, title, when, quote}`, `categories`
and `sens` for each line.

MCP-style tool call:

```bash
curl -s -X POST 'https://falkor.fally.online/mcp/dev' -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_context","arguments":{"question":"Where does Oren live now?"}}}'
```

In Fally the user and the allowed categories come from the OAuth grant of the connection; in this demo they come
from the URL (`/mcp/{user}?categories=people,plans,...`).

## Run it locally

Needs Docker with Compose v2.

```bash
git clone https://github.com/Vishnu-AIR/fally-memory-falkordb
cd fally-memory-falkordb
docker compose up -d --build              # FalkorDB + API
docker compose exec api python -m scripts.seed   # load the 3 synthetic users
open http://localhost:8000                 # the demo page
docker compose exec api pytest -q          # tests against the FalkorDB container
```

(`make up`, `make seed`, `make test` do the same.) FalkorDB is not published to the host; the API reaches it on
the compose network.

## Tests

`tests/test_graph.py` runs against the real FalkorDB container (17 tests): isolation between users, bad user ids,
strong-key resolution and "same as?" candidates, commitments by key only, functional edge closing and restart,
contested claims, idempotent weights, merge and exact undo, never merging Self, neighbourhood never through Self,
bounded path avoiding Self, open-between, the privacy filter (categories, `sens`, the relationships switch), forget,
"which person?" for a shared first name, the synthetic seed end to end, and the MCP tool over HTTP.

## Live demo deployment

One AWS `t4g.small` (arm64), Docker Compose with FalkorDB (not exposed), the API, and Caddy for HTTPS
(`docker-compose.aws.yml`, `Caddyfile`). Only ports 80 and 443 are open; there is no SSH. On the live demo, writes
are limited to the synthetic users and "Reset data" reloads them.

```bash
DOMAIN=falkor.fally.online docker compose -f docker-compose.yml -f docker-compose.aws.yml up -d --build
docker compose exec api python -m scripts.seed
```

## FalkorDB notes

- Fixed-length variable-length patterns (`-[:T*2]-`, `-[*2..2]-`, min equal to max and above 1) returned no rows
  with a relationship type, and crashed the server without one, on v4.22.0 and v4.22.1 (arm64). The path query
  uses `*1..k` with `ORDER BY length(p) LIMIT 1` instead.
- Properties cannot be maps, so free-form extractor props are stored as a JSON string and the fields Cypher filters
  on are promoted to real properties.
- Validity periods are ISO-8601 UTC strings compared as strings.

## AI tools used

Claude Code helped write the code and the docs.

## Origin

The model is Fally's production memory graph (Postgres), re-implemented here from scratch in Python and Cypher on
FalkorDB for the hackathon. Production Fally is not part of this repo.
