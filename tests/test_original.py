"""Serving the original back.

Everything else Orpheus shows is its own reading of a document: text with page
markers, instances, excerpts, a wiki page. A reviewer confirming a finding is
entitled to the thing all of that was derived from, and until now the file went
into content-addressed storage and never came out again.

The whole of this is in one word of the promise `ingest` opens with -- that the
original is kept "so an extraction can always be re-run against exactly the
bytes it was derived from". A path that resolves does not establish *exactly*.
The hash does, and it is also what makes reading a path out of a database
column safe: nothing an attacker can point that column at will hash to a digest
recorded before they got there.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from orpheus import api, ingest
from orpheus.ingest import OriginalUnavailable, ingest as ingest_file, original
from orpheus.utils import NotFound

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
import orpheus_datasette as plugin  # noqa: E402

CONTENT = "SERVICES AGREEMENT\n\nbetween Ardmore Digital Limited and the HSE.\n"


@pytest.fixture
def filed(store, tmp_path):
    """One ingested document, and the actor who ingested it."""
    store.insert("actors", {"actor_id": "act_test", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    source = tmp_path / "services-agreement.txt"
    source.write_text(CONTENT)
    result = ingest_file(store, source, actor_id="act_test",
                         storage_root=tmp_path / "storage")
    return {"store": store, "document_id": result["document_id"],
            "root": tmp_path / "storage", "tmp": tmp_path}


def _stored(filed) -> Path:
    return Path(filed["store"].one(
        "SELECT storage_path FROM documents WHERE document_id = ?",
        (filed["document_id"],))["storage_path"])


# -- locating it -------------------------------------------------------------

def test_the_bytes_that_come_back_are_the_bytes_that_went_in(filed):
    located = original(filed["store"], filed["document_id"])
    assert located["path"].read_text() == CONTENT
    assert located["filename"] == "services-agreement.txt"
    assert located["byte_size"] == len(CONTENT.encode())
    assert located["verified"] is True


def test_an_unknown_document_is_not_found(filed):
    with pytest.raises(NotFound):
        original(filed["store"], "doc_nothing")


def test_a_row_with_no_stored_path_says_so(filed):
    filed["store"].execute("UPDATE documents SET storage_path = NULL")
    with pytest.raises(OriginalUnavailable) as raised:
        original(filed["store"], filed["document_id"])
    assert raised.value.reason == "not_stored"


def test_a_pruned_file_is_reported_rather_than_raising_oserror(filed):
    """Nothing in Orpheus deletes, but storage is a directory on a disk that
    somebody else's retention policy can reach. The row outliving the file is
    a state this has to have an answer for."""
    _stored(filed).unlink()
    with pytest.raises(OriginalUnavailable) as raised:
        original(filed["store"], filed["document_id"])
    assert raised.value.reason == "missing"
    assert "The row is intact" in str(raised.value)


# -- the two that matter -----------------------------------------------------

def test_a_poisoned_path_column_cannot_read_an_arbitrary_file(filed, tmp_path):
    """The reason this function does not simply open `storage_path`.

    That column is a path in a database, and a database is a thing that gets
    written to -- by a future bug, a future migration, or anyone who reaches
    the write connection. Serving whatever is at the end of it would turn one
    write into an arbitrary file read.
    """
    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN PRIVATE KEY-----\n")
    filed["store"].execute("UPDATE documents SET storage_path = ?", (str(secret),))

    with pytest.raises(OriginalUnavailable) as raised:
        original(filed["store"], filed["document_id"])
    assert raised.value.reason == "misfiled"
    # Refused on the layout, before anything was read: content-addressed
    # storage puts a document at exactly one path, so a different one is wrong
    # without needing to know what is in it.
    assert "no document of that hash belongs at" in str(raised.value)


def test_a_path_inside_storage_still_has_to_be_the_right_bytes(filed):
    """The layout check is the cheap half. Somewhere under the storage root
    with the right name is a shape an attacker who can write the column *can*
    produce, and a backup restored from the wrong moment produces it by
    accident. The hash is what actually settles it."""
    _stored(filed).write_text("A DIFFERENT AGREEMENT ENTIRELY\n")
    with pytest.raises(OriginalUnavailable) as raised:
        original(filed["store"], filed["document_id"])
    assert raised.value.reason == "altered"
    message = str(raised.value)
    assert "not the file that was ingested" in message
    # Says which two digests disagree, because an operator's next question is
    # whether this is the wrong file or a corrupted one.
    assert "recorded" in message and "found" in message


def test_skipping_the_hash_does_not_skip_the_layout_check(filed, tmp_path):
    """`verify=False` exists for a caller that has already read the file. It is
    a performance choice, not a permission to open any path in the column."""
    altered = _stored(filed)
    altered.write_text("changed")
    assert original(filed["store"], filed["document_id"],
                    verify=False)["verified"] is False

    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN PRIVATE KEY-----\n")
    filed["store"].execute("UPDATE documents SET storage_path = ?", (str(secret),))
    with pytest.raises(OriginalUnavailable) as raised:
        original(filed["store"], filed["document_id"], verify=False)
    assert raised.value.reason == "misfiled"


# -- over the API ------------------------------------------------------------

def _actor(store, actor_id="act_test"):
    return dict(store.one("SELECT * FROM actors WHERE actor_id = ?", (actor_id,)))


def test_the_route_returns_a_file_body_not_json(filed):
    status, payload = api.handle(
        filed["store"], "GET", f"/documents/{filed['document_id']}/original",
        actor=_actor(filed["store"]))
    assert status == 200
    assert isinstance(payload, api.FileBody)
    assert payload.path.read_text() == CONTENT
    assert payload.media_type == "text/plain"


def test_metadata_answers_without_the_file(filed):
    status, payload = api.handle(
        filed["store"], "GET", f"/documents/{filed['document_id']}/original",
        body={"metadata": "1"}, actor=_actor(filed["store"]))
    assert status == 200
    assert payload["available"] is True
    assert payload["byte_size"] == len(CONTENT.encode())
    # Not the path: a client deciding whether to fetch fifty megabytes needs
    # the size, not the server's filesystem layout.
    assert "path" not in payload


def test_a_store_that_disagrees_with_its_disk_is_a_conflict_not_a_404(filed):
    """404 would say the document is not there and 400 would blame the caller.
    Neither is true, and an operator has to see this one."""
    _stored(filed).write_text("not the same file")
    status, payload = api.handle(
        filed["store"], "GET", f"/documents/{filed['document_id']}/original",
        actor=_actor(filed["store"]))
    assert status == 409
    assert payload["error"]["reason"] == "altered"


def test_a_missing_file_is_a_404(filed):
    _stored(filed).unlink()
    status, payload = api.handle(
        filed["store"], "GET", f"/documents/{filed['document_id']}/original",
        actor=_actor(filed["store"]))
    assert status == 404
    assert payload["error"]["reason"] == "missing"


def test_an_actor_without_view_is_refused_before_the_file_is_touched(filed):
    filed["store"].insert("actors", {
        "actor_id": "act_other", "display_name": "Bo", "is_admin": 0,
        "created_at": "2026-01-01T00:00:00Z"})
    status, payload = api.handle(
        filed["store"], "GET", f"/documents/{filed['document_id']}/original",
        actor=_actor(filed["store"], "act_other"))
    assert status == 403
    assert not isinstance(payload, api.FileBody)


def test_the_document_payload_says_whether_the_original_is_there(filed):
    _, document = api.handle(
        filed["store"], "GET", f"/documents/{filed['document_id']}",
        actor=_actor(filed["store"]))
    assert document["original"]["available"] is True

    _stored(filed).unlink()
    _, gone = api.handle(
        filed["store"], "GET", f"/documents/{filed['document_id']}",
        actor=_actor(filed["store"]))
    assert gone["original"] == {"available": False, "reason": "missing",
                                "message": gone["original"]["message"]}


def test_the_page_check_does_not_hash_the_file(filed, monkeypatch):
    """A page load that hashed a fifty-megabyte PDF to decide whether to draw a
    link would be a page load nobody waits for. The download hashes; the page
    only asks whether a file is there."""
    monkeypatch.setattr(ingest, "hash_file",
                        lambda path: pytest.fail("hashed on a page load"))
    _, document = api.handle(
        filed["store"], "GET", f"/documents/{filed['document_id']}",
        actor=_actor(filed["store"]))
    assert document["original"]["available"] is True


# -- the response ------------------------------------------------------------

@pytest.mark.parametrize("filename,expected", [
    # A newline would end the header and start another one.
    ("a\r\nX-Injected: 1.pdf", 'filename="a__X-Injected: 1.pdf"'),
    # A separator would suggest a path where there is only a name.
    ("../../etc/passwd", 'filename="passwd"'),
    ('quote".pdf', 'filename="quote_.pdf"'),
    ("..", 'filename="download"'),
    ("", 'filename="download"'),
])
def test_a_filename_never_becomes_a_second_header(filename, expected):
    disposition = plugin._disposition(filename, "application/pdf", False)
    assert expected in disposition
    assert "\r" not in disposition and "\n" not in disposition


def test_a_name_that_is_not_ascii_survives_in_the_second_form():
    disposition = plugin._disposition("conträct.pdf", "application/pdf", False)
    assert 'filename="contr?ct.pdf"' in disposition
    assert "filename*=UTF-8''contr%C3%A4ct.pdf" in disposition


@pytest.mark.parametrize("media_type,how", [
    ("application/pdf", "inline"),
    ("image/png", "inline"),
    ("text/plain", "inline"),
    # An SVG is a document that can carry script. Rendering one inline would
    # run it on Datasette's origin with the reviewer's session.
    ("image/svg+xml", "attachment"),
    ("text/html", "attachment"),
    ("application/octet-stream", "attachment"),
])
def test_only_types_a_browser_renders_safely_are_shown_inline(media_type, how):
    assert plugin._disposition("x", media_type, False).startswith(how)


def test_download_forces_an_attachment_whatever_the_type():
    assert plugin._disposition("x.pdf", "application/pdf", True) \
        .startswith("attachment")


class _Request:
    """`_send_file` reads two things off a request: the conditional header and
    whether `?download` was asked for."""

    def __init__(self, headers=None, args=()):
        self.headers = headers or {}
        self.args = args


def _body(filed) -> api.FileBody:
    _, payload = api.handle(
        filed["store"], "GET", f"/documents/{filed['document_id']}/original",
        actor=_actor(filed["store"]))
    return payload


def test_the_response_carries_the_file_and_refuses_to_be_sniffed(filed):
    response = plugin._send_file(_Request(), _body(filed), download=False)
    assert response.status == 200
    assert response.body == CONTENT.encode()
    assert response.content_type == "text/plain"
    # Without this a browser may decide a mislabelled file is HTML and render
    # it here, on this origin, with this reviewer's session.
    assert response.headers["x-content-type-options"] == "nosniff"
    # `private` because a shared cache holding a permissioned document is the
    # whole problem; `no-cache` so the permission check runs every time.
    assert response.headers["cache-control"] == "private, no-cache"


def test_the_etag_is_the_documents_own_digest(filed):
    body = _body(filed)
    response = plugin._send_file(_Request(), body, download=False)
    assert response.headers["etag"] == f'"{body.file_hash}"'


def test_a_client_that_already_has_it_gets_no_bytes_back(filed):
    """Content-addressed storage means a digest is one sequence of bytes
    forever, so a client holding the ETag needs nothing else -- and a reviewer
    flipping between the text and a thirty-megabyte PDF pays once."""
    body = _body(filed)
    request = _Request(headers={"if-none-match": f'"{body.file_hash}"'})
    response = plugin._send_file(request, body, download=False)
    assert response.status == 304
    assert response.body == ""


def test_a_stale_etag_is_served_in_full(filed):
    request = _Request(headers={"if-none-match": '"something-else"'})
    response = plugin._send_file(request, _body(filed), download=False)
    assert response.status == 200
    assert response.body == CONTENT.encode()


# -- auditing the corpus -----------------------------------------------------

@pytest.fixture
def three(store, tmp_path):
    """Three ingested documents, so an audit has something to be partial about."""
    store.insert("actors", {"actor_id": "act_test", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    ids = []
    for n in range(3):
        source = tmp_path / f"doc-{n}.txt"
        source.write_text(f"{CONTENT}Document {n}.\n")
        ids.append(ingest_file(store, source, actor_id="act_test",
                               storage_root=tmp_path / "storage")["document_id"])
    return {"store": store, "ids": ids, "tmp": tmp_path}


def _path_of(store, document_id) -> Path:
    return Path(store.one("SELECT storage_path FROM documents "
                          "WHERE document_id = ?", (document_id,))["storage_path"])


def test_a_clean_corpus_does_not_claim_more_than_it_checked(three):
    """A `stat` establishes that a file exists. Saying a corpus is sound on
    that basis is the reassurance this project keeps refusing to give."""
    audit = ingest.audit_storage(three["store"], verify=False)
    assert audit["n_documents"] == 3 and audit["n_unavailable"] == 0
    assert "Nothing was read" in audit["headline"]
    assert "orpheus verify" in audit["headline"]

    verified = ingest.audit_storage(three["store"], verify=True)
    assert "hash to the digests recorded at ingest" in verified["headline"]
    assert verified["bytes_read"] > 0


def test_the_audit_counts_each_kind_of_loss_separately(three):
    store, (a, b, c) = three["store"], three["ids"]
    _path_of(store, a).unlink()
    store.execute("UPDATE documents SET storage_path = NULL WHERE document_id = ?",
                  (b,))
    audit = ingest.audit_storage(store, verify=False)
    assert audit["reasons"] == {"missing": 1, "not_stored": 1}
    assert audit["n_available"] == 1
    assert "Every excerpt taken from them is now unverifiable" in audit["headline"]
    # The dangerous case is not the clean one. A quick pass that finds two
    # problems reads like a complete answer and is not, so the caveat goes on
    # both.
    assert "Nothing was read" in audit["headline"]


def test_only_the_hashing_pass_sees_a_file_whose_bytes_changed(three):
    """The distinction the two passes exist for. A restored corpus from the
    wrong moment has every file present and the wrong contents, and the cheap
    pass reports it as sound."""
    store, (a, _, _) = three["store"], three["ids"]
    _path_of(store, a).write_text("a different document entirely")

    assert ingest.audit_storage(store, verify=False)["n_unavailable"] == 0
    hashed = ingest.audit_storage(store, verify=True)
    assert hashed["reasons"] == {"altered": 1}


def test_the_lint_locates_a_lost_original(three):
    from orpheus import lint as lint_mod
    store, (a, _, _) = three["store"], three["ids"]
    _path_of(store, a).unlink()

    findings = lint_mod.unavailable_originals(store)
    assert len(findings) == 1
    assert findings[0]["where"]["document_id"] == a
    assert findings[0]["severity"] == "medium"

    # And it is cheap enough to be in the pass that runs by default.
    assert "unavailable_original" in lint_mod.SHALLOW
    report = lint_mod.lint(store, deep=False)
    assert any(f["check"] == "unavailable_original" for f in report["findings"])


def test_a_path_nothing_in_this_codebase_would_write_is_rated_higher(three):
    """Same reasoning as `uncited_page`: not "something is missing" but
    "something wrote where only ingest writes"."""
    from orpheus import lint as lint_mod
    store, (a, _, _) = three["store"], three["ids"]
    store.execute("UPDATE documents SET storage_path = '/etc/passwd' "
                  "WHERE document_id = ?", (a,))
    findings = lint_mod.unavailable_originals(store)
    assert findings[0]["severity"] == "high"
    assert "did not come from this codebase" in findings[0]["suggestion"]


def test_the_lint_does_not_read_a_single_file(three, monkeypatch):
    """It runs on every lint, so it has to stay a stat per document."""
    from orpheus import lint as lint_mod
    monkeypatch.setattr(ingest, "hash_file",
                        lambda path: pytest.fail("the lint hashed a file"))
    assert lint_mod.unavailable_originals(three["store"]) == []


def test_hashing_the_whole_corpus_is_refused_over_http(three):
    """It would hold the connection Datasette answers pages on."""
    status, payload = api.handle(
        three["store"], "GET", "/storage/audit", body={"verify": "1"},
        actor=_actor(three["store"]))
    assert status == 400
    assert "orpheus verify" in payload["error"]["message"]


def test_one_document_may_be_verified_over_http(three):
    store, (a, _, _) = three["store"], three["ids"]
    _path_of(store, a).write_text("not the same file")
    status, payload = api.handle(
        store, "GET", "/storage/audit",
        body={"verify": "1", "document_id": a}, actor=_actor(store))
    assert status == 200
    assert payload["reasons"] == {"altered": 1}


def test_the_audit_is_an_administrators_view(three):
    three["store"].insert("actors", {
        "actor_id": "act_other", "display_name": "Bo", "is_admin": 0,
        "created_at": "2026-01-01T00:00:00Z"})
    status, _ = api.handle(three["store"], "GET", "/storage/audit",
                           actor=_actor(three["store"], "act_other"))
    assert status == 403
