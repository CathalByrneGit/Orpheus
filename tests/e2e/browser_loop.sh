#!/usr/bin/env bash
# The Phase 1 loop, driven through a real Datasette over HTTP.
#
# The pytest suite calls the core directly, which cannot catch anything that
# only goes wrong once Datasette is in the middle: its multipart parser and its
# limits, its CSRF token, its write queue and the transaction that queue opens
# around every task. All four have broken this plugin at least once.
#
#   tests/e2e/browser_loop.sh [port]
#
# Exits non-zero on the first failed step. Leaves the store in a temp directory
# named on stdout so the result can be inspected.
set -euo pipefail

PORT="${1:-8011}"
BASE="http://127.0.0.1:$PORT"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
PDF="$ROOT/tests/fixtures/services-agreement.pdf"
CURL=(curl -sS --noproxy '*' -b "$WORK/cookies" -c "$WORK/cookies")

cleanup() { [ -n "${SERVER:-}" ] && kill "$SERVER" 2>/dev/null || true; }
trap cleanup EXIT

say() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

say "store in $WORK"
cd "$ROOT"
python3 - "$WORK" <<'PY'
import sys
from orpheus.store import Store
from orpheus import bundle as bundle_mod
from orpheus.datasette_config import write_config

work = sys.argv[1]
store = Store(f"{work}/orpheus.sqlite", mode="write")
bundle = bundle_mod.load()
bundle_mod.register(store, bundle)
bundle_mod.apply_schema(store, bundle)
with store.transaction():
    store.execute("INSERT INTO actors (actor_id, display_name, is_admin, created_at)"
                  " VALUES (?,?,?,datetime('now'))", ("act_demo", "Demo", 1))
store.set_setting("cloud_ai_policy", "org_allow", "act_demo")
store.close()
write_config(f"{work}/datasette.yml", storage_root=f"{work}/storage")

# Datasette answers "who is this"; --root gives an actor id of "root", and the
# store's actors are its own. actor_map is the seam between them, and the seam
# is worth exercising rather than working around.
path = f"{work}/datasette.yml"
text = open(path).read().replace(
    "    max_file_size:", "    actor_map:\n      root: act_demo\n    max_file_size:")
open(path, "w").write(text)
PY

say "starting datasette on $PORT"
python3 -m datasette serve "$WORK/orpheus.sqlite" \
  --metadata "$WORK/metadata.yml" --config "$WORK/datasette.yml" \
  --plugins-dir plugins --template-dir templates \
  --port "$PORT" --root --secret e2e > "$WORK/server.log" 2>&1 &
SERVER=$!

for _ in $(seq 1 40); do
  curl -sS --noproxy '*' -o /dev/null "$BASE/-/orpheus" 2>/dev/null && break
  kill -0 "$SERVER" 2>/dev/null || { cat "$WORK/server.log"; fail "server exited"; }
  python3 -c 'import time; time.sleep(0.25)'
done

TOKEN_URL="$(grep -o "$BASE/-/auth-token?token=[a-f0-9]*" "$WORK/server.log" | head -1)"
[ -n "$TOKEN_URL" ] || fail "no sign-in URL in the log"
"${CURL[@]}" -o /dev/null "$TOKEN_URL"

say "the index page renders, with the capabilities the store actually has"
"${CURL[@]}" "$BASE/-/orpheus" > "$WORK/index.html"
grep -q "Add a document" "$WORK/index.html" || fail "no upload form"
grep -q "contract-core" "$WORK/index.html" || fail "the active bundle is not named"
CSRF="$(grep -o 'name="csrftoken" value="[^"]*"' "$WORK/index.html" | head -1 |
        sed 's/.*value="//;s/"//')"
[ -n "$CSRF" ] || fail "no CSRF token"

say "a file over the limit is refused as a message, not a 400 page"
python3 -c "open('$WORK/big.pdf','wb').write(b'%PDF-1.4\n' + b'0' * (60*1024*1024))"
LOC="$("${CURL[@]}" -o /dev/null -D - -F "csrftoken=$CSRF" -F "file=@$WORK/big.pdf" \
        -F "tier=local" "$BASE/-/orpheus/upload" | tr -d '\r' |
      awk 'tolower($1)=="location:"{print $2}')"
case "$LOC" in
  *"error=Upload+rejected"*) ;;
  *) fail "an oversized upload redirected to '$LOC', expected a rejection" ;;
esac
rm -f "$WORK/big.pdf"

say "uploading a real PDF through the form"
LOC="$("${CURL[@]}" -o /dev/null -D - -F "csrftoken=$CSRF" -F "file=@$PDF" \
        -F "tier=local" -F "engine=auto" "$BASE/-/orpheus/upload" | tr -d '\r' |
      awk 'tolower($1)=="location:"{print $2}')"
case "$LOC" in
  /-/orpheus/document/doc_*) ;;
  *) fail "upload redirected to '$LOC'" ;;
esac
DOC="${LOC#/-/orpheus/document/}"; DOC="${DOC%%\?*}"
say "ingested as $DOC"

say "the deterministic pass found things, and they are grounded"
INSTANCES="$("${CURL[@]}" "$BASE/-/orpheus/api/documents/$DOC/instances")"
python3 - "$INSTANCES" <<'PY'
import json, sys
found = json.loads(sys.argv[1])["instances"]
assert found, "nothing was extracted"
for i in found:
    assert i["status"] == "unconfirmed", i
    assert i["provenance_source"], f"no provenance on {i['instance_id']}"
    assert i["excerpt"], f"no excerpt on {i['instance_id']}"
    assert i["page_no"], f"not located on a page: {i['instance_id']}"
dates = [i for i in found if i["type_id"] == "KeyDate"]
assert any(i["properties"]["date_role"] == "start" for i in dates), \
    "no date was read as the start date"
assert any(i["properties"]["date_role"] == "end" for i in dates), \
    "no date was read as the end date -- the cue stems regressed"
print(f"   {len(found)} instances, {len(dates)} dates")
PY

say "the document page renders every finding with its excerpt"
"${CURL[@]}" "$BASE/-/orpheus/document/$DOC" > "$WORK/doc.html"
grep -q "Extracted facts" "$WORK/doc.html" || fail "no findings section"
grep -q "unconfirmed" "$WORK/doc.html" || fail "nothing is awaiting review"

read -r FIRST SECOND THIRD <<<"$(printf '%s' "$INSTANCES" | python3 -c \
  "import json,sys; print(*(i['instance_id'] for i in json.load(sys.stdin)['instances'][:3]))")"

post_review() {
  "${CURL[@]}" -o /dev/null -D - -X POST \
    --data-urlencode "csrftoken=$CSRF" --data-urlencode "document_id=$DOC" "$@" \
    "$BASE/-/orpheus/review" | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}'
}

say "confirming, amending and rejecting through the form"
post_review --data-urlencode "instance_id=$FIRST" --data-urlencode "action=confirm" \
  --data-urlencode "note=checked against the signature page" | grep -q "confirmed" \
  || fail "confirm did not report success"
post_review --data-urlencode "instance_id=$SECOND" --data-urlencode "action=amend" \
  --data-urlencode "change_date_role=end" --data-urlencode "note=clause 1" \
  | grep -q "amended" || fail "amend did not report success"
post_review --data-urlencode "instance_id=$THIRD" --data-urlencode "action=reject" \
  --data-urlencode "note=a clause number, not a date" \
  | grep -q "rejected" || fail "reject did not report success"

say "an amendment that changes nothing is refused, not recorded"
post_review --data-urlencode "instance_id=$SECOND" --data-urlencode "action=amend" \
  --data-urlencode "change_date_role=end" | grep -q "error=" \
  || fail "a no-op amendment was accepted"

say "a write that fails leaves nothing behind"
STATUS="$("${CURL[@]}" -o /dev/null -w '%{http_code}' -X POST \
  -H 'content-type: application/json' -d '{"path": "/nope/missing.pdf"}' \
  "$BASE/-/orpheus/api/documents")"
[ "$STATUS" = "400" ] || fail "ingesting a missing file returned $STATUS, expected 400"

say "the store agrees with what the browser was told"
python3 - "$WORK" "$DOC" "$FIRST" "$SECOND" "$THIRD" <<'PY'
import sqlite3, sys
work, doc, first, second, third = sys.argv[1:6]
conn = sqlite3.connect(f"{work}/orpheus.sqlite")
conn.row_factory = sqlite3.Row
q = lambda sql, *p: conn.execute(sql, p).fetchall()

assert len(q("SELECT 1 FROM documents")) == 1, "the failed ingest left a row behind"

status = {r["instance_id"]: r["status"] for r in
          q("SELECT instance_id, status FROM instances_KeyDate")}
assert status.get(first) == "confirmed", status
assert status.get(second) == "amended", status
assert status.get(third) == "rejected", status
# Rejected, not deleted: a rejected extraction is evidence about extraction
# quality, and deleting it would throw away the measurement with the mistake.
assert q("SELECT 1 FROM instances_KeyDate WHERE instance_id = ?", third), \
    "the rejected row was deleted"

# Provenance is the immutable record of what the machine said. Review must not
# have touched it.
for row in q("SELECT source, confidence FROM provenance"):
    assert row["source"] == "ai_local", dict(row)

# One amend recorded, not two: the no-op was refused.
amends = q("SELECT * FROM edit_history WHERE action = 'amend'")
assert len(amends) == 1, f"{len(amends)} amendments recorded, expected 1"

actions = [r["action"] for r in q("SELECT action FROM edit_history ORDER BY seq")]
assert actions[0] == "ingest", actions
assert {"extract", "confirm", "amend", "reject"} <= set(actions), actions
# Ordered by seq, never by timestamp: three reviews inside one second would
# otherwise be unorderable, and the history is the record of what happened when.
seqs = [r["seq"] for r in q("SELECT seq FROM edit_history ORDER BY rowid")]
assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1)), seqs

# Every write went through Datasette's write thread. A second writer would have
# left its own advisory lock file next to the database.
import os
assert not os.path.exists(f"{work}/orpheus.sqlite.orpheus-writer"), \
    "something opened a second writer"
print("   store checks passed")
PY

say "the wiki: propose, then read a page"
# Templates are only exercised by rendering them; a missing variable or a bad
# filter is invisible to the unit tests, which call the projection directly.
"${CURL[@]}" "$BASE/-/orpheus/wiki" > "$WORK/wiki.html"
grep -q "What needs doing" "$WORK/wiki.html" || fail "the wiki front page did not render"
# `|| true`: a grep that matches nothing exits 1, and under `set -e` that kills
# the script from inside a command substitution with no message at all.
WIKI_CSRF="$(grep -o 'name="csrftoken" value="[^"]*"' "$WORK/wiki.html" | head -1 |
             sed 's/.*value="//;s/"//' || true)"
[ -n "$WIKI_CSRF" ] || fail "no CSRF token on the wiki page"
"${CURL[@]}" -o /dev/null -X POST --data-urlencode "csrftoken=$WIKI_CSRF" \
  --data-urlencode "action=propose" "$BASE/-/orpheus/wiki/act"

"${CURL[@]}" "$BASE/-/orpheus/wiki" > "$WORK/wiki2.html"
ENT="$(grep -o '/-/orpheus/wiki/ent_[a-f0-9]*' "$WORK/wiki2.html" | head -1 |
       sed 's#.*/##' || true)"
if [ -n "$ENT" ]; then
  "${CURL[@]}" "$BASE/-/orpheus/wiki/$ENT" > "$WORK/entity.html"
  grep -q "What the documents say" "$WORK/entity.html" || fail "entity page did not render"
  grep -q "Sources" "$WORK/entity.html" || fail "entity page has no sources section"
  say "  rendered a page for $ENT"
else
  say "  no Named instances extracted, so no pages -- the deterministic pass"
  say "  finds dates and amounts, which carry no name"
fi

"${CURL[@]}" "$BASE/-/orpheus/wiki/queue" > "$WORK/queue.html"
grep -q "Mentions with no page" "$WORK/queue.html" || fail "the queue did not render"

say "the loop ran clean, and the server never fell over"
if grep -q "Traceback" "$WORK/server.log"; then
  echo "--- server log ---"; cat "$WORK/server.log"
  fail "the server logged a traceback"
fi
kill -0 "$SERVER" 2>/dev/null || fail "the server is no longer running"
say "OK -- store left at $WORK"
