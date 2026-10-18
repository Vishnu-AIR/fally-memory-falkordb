"""Every Cypher query the memory graph runs, in one place.

All queries run against ONE user's graph (FalkorDB `select_graph("fally_user_<id>")`), so none of them needs a
`user_id` filter: another user's data is simply not in the graph being queried.

Placeholders in braces ({REL}, {LABEL}, {ENTITY_RELS}) are filled from the whitelists in model.py, because Cypher
has no parameters for labels or relationship types. Everything else is a $parameter.
"""

# ---------------------------------------------------------------- schema
SCHEMA = [
    "CREATE INDEX FOR (n:Entity) ON (n.uid)",
    "CREATE INDEX FOR (n:Entity) ON (n.type)",
    "CREATE INDEX FOR (m:Mention) ON (m.uid)",
    "CREATE INDEX FOR (m:Mention) ON (m.target_uid)",
    "CREATE INDEX FOR (s:Source) ON (s.source_id)",
    "CREATE INDEX FOR (x:Merge) ON (x.uid)",
    # full-text (RediSearch-backed) indexes: question words -> quotes, names with fuzzy matching
    "CALL db.idx.fulltext.createNodeIndex('Mention', 'quote')",
    "CALL db.idx.fulltext.createNodeIndex('Entity', 'search_text')",
]

# ---------------------------------------------------------------- nodes
ENSURE_SELF = """
MERGE (s:Entity:Self {type: 'self'})
ON CREATE SET s.uid = $uid, s.name = $name, s.names_norm = [$norm], s.aliases = [], s.keys = [],
              s.search_text = $name, s.first_seen = $now, s.last_seen = $now, s.importance = 1.0,
              s.categories = ['about_me'], s.sens = 0, s.same_as = [], s.props = '{}'
RETURN s.uid AS uid, s.name AS name
"""

GET_SELF = "MATCH (s:Self) RETURN s.uid AS uid, s.name AS name"

# candidates for resolution: active nodes of one type sharing a strong key
NODES_BY_KEYS = """
MATCH (n:Entity {type: $type})
WHERE n.merged_into IS NULL AND any(k IN n.keys WHERE k IN $keys)
RETURN n.uid AS uid, n.name AS name, n.aliases AS aliases, n.keys AS keys
ORDER BY n.first_seen
"""

# active nodes of one type that share a normalised name or alias (exact)
NODES_BY_NAMES = """
MATCH (n:Entity {type: $type})
WHERE n.merged_into IS NULL AND any(x IN n.names_norm WHERE x IN $names)
RETURN n.uid AS uid, n.name AS name, n.aliases AS aliases, n.keys AS keys
"""

# fuzzy name search (full-text index, Levenshtein distance 1 with %term%) for "same as?" candidates
FUZZY_NAMES = """
CALL db.idx.fulltext.queryNodes('Entity', $q) YIELD node, score
WHERE node.type = $type AND node.merged_into IS NULL
RETURN node.uid AS uid, node.name AS name, node.aliases AS aliases, node.keys AS keys, score
LIMIT 10
"""

ALL_ACTIVE_OF_TYPE = """
MATCH (n:Entity {type: $type}) WHERE n.merged_into IS NULL
RETURN n.uid AS uid, n.name AS name, n.aliases AS aliases, n.keys AS keys
"""

CREATE_NODE = """
CREATE (n:Entity:{LABEL})
SET n += $props
RETURN n.uid AS uid
"""

TOUCH_NODE = """
MATCH (n:Entity {uid: $uid})
SET n.aliases = $aliases, n.keys = $keys, n.names_norm = $names_norm, n.search_text = $search_text,
    n.last_seen = CASE WHEN n.last_seen < $at THEN $at ELSE n.last_seen END,
    n.first_seen = CASE WHEN n.first_seen > $at THEN $at ELSE n.first_seen END,
    n.importance = n.importance + 1.0,
    n.categories = $categories, n.sens = CASE WHEN n.sens > $sens THEN n.sens ELSE $sens END,
    n.props = $props_json
"""

SET_NODE_FIELDS = """
MATCH (n:Entity {uid: $uid})
SET n += $fields
"""

GET_NODE = """
MATCH (n:Entity {uid: $uid})
RETURN n.uid AS uid, n.type AS type, n.name AS name, n.aliases AS aliases, n.keys AS keys,
       n.names_norm AS names_norm, n.same_as AS same_as, n.merged_into AS merged_into, n.props AS props,
       n.categories AS categories, n.sens AS sens, n.first_seen AS first_seen, n.last_seen AS last_seen,
       n.importance AS importance, n.search_text AS search_text
"""

# follow merge pointers (a merged node points at the node it was merged into)
CANONICAL = """
MATCH (n:Entity {uid: $uid})
OPTIONAL MATCH (m:Entity {uid: n.merged_into})
OPTIONAL MATCH (m2:Entity {uid: m.merged_into})
RETURN n.uid AS a, n.merged_into AS b, m.merged_into AS c
"""

# ---------------------------------------------------------------- provenance
UPSERT_SOURCE = """
MERGE (s:Source {source_id: $source_id})
ON CREATE SET s.source_type = $source_type, s.title = $title, s.ts = $ts, s.categories = $categories
RETURN s.source_id AS source_id
"""

# a quote about a node: (entity)-[:MENTIONED_IN]->(mention)-[:FROM]->(source)
NODE_MENTION = """
MATCH (n:Entity {uid: $target_uid}), (s:Source {source_id: $source_id})
CREATE (n)-[:MENTIONED_IN]->(m:Mention {uid: $uid, target_kind: 'node', target_uid: $target_uid, quote: $quote,
                                       ts: $ts, sens: $sens, categories: $categories})-[:FROM]->(s)
RETURN m.uid AS uid
"""

# a quote about an edge (relationships cannot have relationships, so the mention holds the edge uid)
EDGE_MENTION = """
MATCH (s:Source {source_id: $source_id})
CREATE (m:Mention {uid: $uid, target_kind: 'edge', target_uid: $target_uid, quote: $quote, ts: $ts,
                   sens: $sens, categories: $categories})-[:FROM]->(s)
RETURN m.uid AS uid
"""

MENTION_EXISTS = """
MATCH (m:Mention {target_uid: $target_uid})-[:FROM]->(s:Source {source_id: $source_id})
RETURN count(m) AS n
"""

WHY_EDGE = """
MATCH (m:Mention {target_uid: $uid})-[:FROM]->(s:Source)
RETURN m.uid AS mention, m.quote AS quote, m.ts AS ts, m.sens AS sens, s.source_id AS source_id,
       s.source_type AS source_type, s.title AS title, s.categories AS categories
ORDER BY m.ts DESC
"""

WHY_NODE = """
MATCH (n:Entity {uid: $uid})-[:MENTIONED_IN]->(m:Mention)-[:FROM]->(s:Source)
RETURN m.uid AS mention, m.quote AS quote, m.ts AS ts, m.sens AS sens, s.source_id AS source_id,
       s.source_type AS source_type, s.title AS title, s.categories AS categories
ORDER BY m.ts DESC
"""

SEARCH_QUOTES = """
CALL db.idx.fulltext.queryNodes('Mention', $q) YIELD node, score
MATCH (node)-[:FROM]->(s:Source)
RETURN node.uid AS mention, node.quote AS quote, node.ts AS ts, node.sens AS sens, node.target_uid AS target_uid,
       node.target_kind AS target_kind, s.source_id AS source_id, s.source_type AS source_type, s.title AS title,
       s.categories AS categories, score
ORDER BY score DESC LIMIT $limit
"""

# ---------------------------------------------------------------- edges
# every edge of one relation (src, dst, type), open and closed periods
EDGES_OF_RELATION = """
MATCH (a:Entity {uid: $src})-[r:{REL}]->(b:Entity {uid: $dst})
RETURN r.uid AS uid, r.valid_from AS valid_from, r.valid_to AS valid_to, r.confidence AS confidence,
       r.basis AS basis, r.weight AS weight, r.sources AS sources, r.contested AS contested,
       r.categories AS categories, r.sens AS sens, r.props AS props
"""

# open edges of a functional type from src to other destinations
OPEN_FUNCTIONAL_OTHERS = """
MATCH (a:Entity {uid: $src})-[r:{REL}]->(b:Entity)
WHERE b.uid <> $dst AND r.valid_to IS NULL
RETURN r.uid AS uid, b.uid AS dst, r.valid_from AS valid_from, r.confidence AS confidence
"""

CREATE_EDGE = """
MATCH (a:Entity {uid: $src}), (b:Entity {uid: $dst})
CREATE (a)-[r:{REL}]->(b)
SET r += $props
RETURN r.uid AS uid
"""

UPDATE_EDGE = """
MATCH ()-[r:{REL} {uid: $uid}]->()
SET r += $fields
"""

CLOSE_EDGE = """
MATCH ()-[r:{REL} {uid: $uid}]->()
SET r.valid_to = $valid_to, r.updated_at = $now
"""

DELETE_EDGE = """
MATCH ()-[r:{REL} {uid: $uid}]->()
DELETE r
"""

# all entity edges touching a node, in both directions, with every property (merge snapshots)
EDGES_OF_NODE = """
MATCH (n:Entity {uid: $uid})-[r:{ENTITY_RELS}]-(o:Entity)
RETURN type(r) AS rel, startNode(r).uid AS src, endNode(r).uid AS dst, o.uid AS other, properties(r) AS props
"""

FIND_EDGE = """
MATCH (a:Entity)-[r]->(b:Entity)
WHERE r.uid = $uid
RETURN type(r) AS rel, a.uid AS src, a.name AS src_name, b.uid AS dst, b.name AS dst_name, properties(r) AS props
"""

REPOINT_EDGE_MENTIONS = """
MATCH (m:Mention {target_uid: $old})
SET m.target_uid = $new
RETURN collect(m.uid) AS moved
"""

REPOINT_MENTIONS_BY_UID = """
MATCH (m:Mention) WHERE m.uid IN $uids
SET m.target_uid = $new
"""

NODE_MENTION_UIDS = """
MATCH (n:Entity {uid: $uid})-[:MENTIONED_IN]->(m:Mention)
RETURN collect(m.uid) AS uids
"""

MOVE_NODE_MENTIONS = """
MATCH (a:Entity {uid: $from})-[x:MENTIONED_IN]->(m:Mention), (b:Entity {uid: $into})
WHERE m.uid IN $uids
CREATE (b)-[:MENTIONED_IN]->(m)
SET m.target_uid = $into
DELETE x
"""

# ---------------------------------------------------------------- merges
CREATE_MERGE = """
CREATE (x:Merge {uid: $uid, from_uid: $from, into_uid: $into, at: $now, snapshot: $snapshot})
RETURN x.uid AS uid
"""

GET_MERGE = """
MATCH (x:Merge {uid: $uid})
RETURN x.uid AS uid, x.from_uid AS from_uid, x.into_uid AS into_uid, x.snapshot AS snapshot,
       x.undone_at AS undone_at
"""

MARK_MERGE_UNDONE = "MATCH (x:Merge {uid: $uid}) SET x.undone_at = $now"

# ---------------------------------------------------------------- readers
PEOPLE = """
MATCH (p:Person) WHERE p.merged_into IS NULL
OPTIONAL MATCH (p)-[w:WORKS_AT]->(o:Entity) WHERE w.valid_to IS NULL
RETURN p.uid AS uid, p.name AS name, p.aliases AS aliases, p.same_as AS same_as, p.last_seen AS last_seen,
       p.importance AS importance, p.sens AS sens, p.categories AS categories, collect(o.name) AS works_at
ORDER BY p.importance DESC, p.name
"""

ENTITIES_FOR_LOOKUP = """
MATCH (n:Entity) WHERE n.merged_into IS NULL AND n.type <> 'self'
RETURN n.uid AS uid, n.type AS type, n.name AS name, n.names_norm AS names_norm, n.sens AS sens,
       n.categories AS categories
"""

# Neighbourhood up to depth 2 over current entity edges. Never walks THROUGH Self (the hub that touches
# everything) or through a merged node; Self may still be an end point.
NEIGHBOURHOOD = """
MATCH p = (c:Entity {uid: $uid})-[:{ENTITY_RELS}*1..{DEPTH}]-(n:Entity)
WHERE n.merged_into IS NULL
  AND all(r IN relationships(p) WHERE r.valid_to IS NULL)
  AND (length(p) = 1 OR (NOT 'Self' IN labels(nodes(p)[1]) AND nodes(p)[1].merged_into IS NULL))
UNWIND relationships(p) AS r
WITH DISTINCT r
RETURN r.uid AS uid, type(r) AS rel, startNode(r).uid AS src, endNode(r).uid AS dst, r.confidence AS confidence,
       r.basis AS basis, r.weight AS weight, r.valid_from AS valid_from, r.sens AS sens, r.categories AS categories,
       r.props AS props
LIMIT $limit
"""

NODES_BY_UIDS = """
MATCH (n:Entity) WHERE n.uid IN $uids
RETURN n.uid AS uid, n.type AS type, n.name AS name, n.sens AS sens, n.categories AS categories,
       n.props AS props, n.same_as AS same_as
"""

# Shortest bounded path (at most {HOPS} hops) over current edges, never through Self or merged nodes in the middle.
# The caller runs HOPS = 1, 2, ... and stops at the first level that finds the target (a bounded BFS).
# Note: `*1..{HOPS}` and not `*{HOPS}`: fixed-length patterns (min == max > 1) return nothing or crash on
# FalkorDB v4.22.0 (see README, "FalkorDB notes").
PATH_AT_DEPTH = """
MATCH p = (a:Entity {uid: $a})-[:{ENTITY_RELS}*1..{HOPS}]-(b:Entity {uid: $b})
WHERE all(r IN relationships(p) WHERE r.valid_to IS NULL)
  AND all(x IN nodes(p) WHERE x.uid = $a OR x.uid = $b OR (NOT 'Self' IN labels(x) AND x.merged_into IS NULL))
RETURN [x IN nodes(p) | {uid: x.uid, name: x.name, type: x.type}] AS nodes,
       [r IN relationships(p) | {uid: r.uid, rel: type(r), src: startNode(r).uid, dst: endNode(r).uid,
                                  sens: r.sens, categories: r.categories}] AS rels, length(p) AS hops
ORDER BY hops ASC
LIMIT 1
"""

# What is open between me and X: promises I made to X (you_owe) and promises X made to me (they_owe).
# (owner)-[:PROMISED]->(c:Commitment)-[:OWED_BY]->(recipient)
OPEN_BETWEEN = """
MATCH (s:Self), (x:Entity {uid: $uid})
MATCH (o:Entity)-[p:PROMISED]->(c:Commitment)-[ob:OWED_BY]->(t:Entity)
WHERE p.valid_to IS NULL AND ob.valid_to IS NULL AND c.merged_into IS NULL AND coalesce(c.status, 'open') = 'open'
  AND ((o.uid = s.uid AND t.uid = x.uid) OR (o.uid = x.uid AND t.uid = s.uid))
RETURN CASE WHEN o.uid = s.uid THEN 'you_owe' ELSE 'they_owe' END AS direction, c.uid AS commitment,
       c.name AS text, c.due AS due, p.valid_from AS since, p.uid AS edge, p.categories AS categories,
       p.sens AS sens, x.name AS person
ORDER BY coalesce(c.due, '9999') ASC
"""

# Every open promise to or from me (the "plans" focus of get_context)
ALL_OPEN_PROMISES = """
MATCH (s:Self)
MATCH (o:Entity)-[p:PROMISED]->(c:Commitment)-[ob:OWED_BY]->(t:Entity)
WHERE p.valid_to IS NULL AND ob.valid_to IS NULL AND c.merged_into IS NULL AND coalesce(c.status, 'open') = 'open'
  AND (o.uid = s.uid OR t.uid = s.uid)
RETURN CASE WHEN o.uid = s.uid THEN 'you_owe' ELSE 'they_owe' END AS direction, c.uid AS commitment,
       c.name AS text, c.due AS due, p.uid AS edge, p.categories AS categories, p.sens AS sens,
       CASE WHEN o.uid = s.uid THEN t.name ELSE o.name END AS person
ORDER BY coalesce(c.due, '9999') ASC
"""

# Current and past edges of one node (get_context "What's true now" / "What changed")
FACTS_OF_NODE = """
MATCH (n:Entity {uid: $uid})-[r:{ENTITY_RELS}]-(o:Entity)
WHERE o.merged_into IS NULL AND coalesce(r.contested, false) = false
RETURN r.uid AS uid, type(r) AS rel, startNode(r).uid AS src, startNode(r).name AS src_name,
       endNode(r).uid AS dst, endNode(r).name AS dst_name, o.type AS other_type, r.valid_from AS valid_from,
       r.valid_to AS valid_to, r.confidence AS confidence, r.basis AS basis, r.weight AS weight,
       r.sens AS sens, r.categories AS categories
ORDER BY r.weight DESC
"""

GRAPH_OVERVIEW = """
MATCH (a:Entity)-[r:{ENTITY_RELS}]->(b:Entity)
WHERE a.merged_into IS NULL AND b.merged_into IS NULL AND r.valid_to IS NULL
RETURN r.uid AS uid, type(r) AS rel, a.uid AS src, b.uid AS dst, r.sens AS sens, r.categories AS categories,
       r.confidence AS confidence
LIMIT $limit
"""

# ---------------------------------------------------------------- forget
# Forget a person: their node, every edge touching it, every quote about it or about those edges.
FORGET_EDGE_UIDS = """
MATCH (n:Entity {uid: $uid})-[r]-(:Entity)
RETURN collect(r.uid) AS uids
"""

FORGET_COMMITMENTS = """
MATCH (n:Entity {uid: $uid})-[:PROMISED|OWED_BY]-(c:Commitment)
RETURN collect(DISTINCT c.uid) AS uids
"""

DELETE_MENTIONS_OF = """
MATCH (m:Mention) WHERE m.target_uid IN $uids
DETACH DELETE m
RETURN count(m) AS n
"""

DELETE_NODES = """
MATCH (n:Entity) WHERE n.uid IN $uids
DETACH DELETE n
RETURN count(n) AS n
"""

DELETE_ORPHAN_SOURCES = """
MATCH (s:Source) WHERE NOT ()-[:FROM]->(s)
DELETE s
RETURN count(s) AS n
"""

CLEAR_SAME_AS = """
MATCH (n:Entity) WHERE $uid IN n.same_as
SET n.same_as = [x IN n.same_as WHERE x <> $uid]
"""

COUNTS = """
MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n
"""
