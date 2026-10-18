#!/usr/bin/env bash
# Minimal repro of a FalkorDB bug we hit while building this project (v4.22.0 and v4.22.1).
# A variable-length pattern with min == max > 1 is broken:
#   typed   -[:W*2]-  returns no rows (should return 1)
#   untyped -[*2]-    crashes the server (signal 11, container exits 139)
# Our code works around it with *1..k plus ORDER BY length(p) LIMIT 1 (see fally_graph/queries.py).
# Usage: scripts/repro_varlen_bug.sh [image]   (needs Docker; runs a throwaway container)
set -u
IMAGE="${1:-falkordb/falkordb:v4.22.1}"
NAME="falkordb-varlen-repro"
docker rm -f "$NAME" >/dev/null 2>&1
docker run -d --name "$NAME" "$IMAGE" >/dev/null
until docker exec "$NAME" redis-cli ping >/dev/null 2>&1; do sleep 1; done
q() { docker exec "$NAME" redis-cli GRAPH.QUERY repro "$1"; }

echo "== create (x)-[:W]->(o)<-[:W]-(z)"
q "CREATE (:E {u:'x'})-[:W]->(:E {u:'o'})<-[:W]-(:E {u:'z'})"
echo "== *1..2 (correct: one row, length 2)"
q "MATCH p=(a:E {u:'x'})-[:W*1..2]-(b:E {u:'z'}) RETURN length(p)"
echo "== typed *2 (bug: no rows)"
q "MATCH p=(a:E {u:'x'})-[:W*2]-(b:E {u:'z'}) RETURN length(p)"
echo "== untyped *2 (bug: server crash)"
q "MATCH p=(a:E {u:'x'})-[*2]-(b:E {u:'z'}) RETURN length(p)"
sleep 1
echo "== container state: $(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}}' "$NAME")"
docker rm -f "$NAME" >/dev/null 2>&1
