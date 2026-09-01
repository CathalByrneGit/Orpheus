"""What falls due.

`KeyDate` has carried a `date_role` since the deterministic pass was written and
`Obligation` a `due_date` since the bundle was authored, and nothing had ever
queried either. Every date in the store was extracted, located on a page, graded
by the rubric, and left there.

The tests here are mostly about what the calendar refuses to do. Turning
extracted dates into a list is easy; the work is in not presenting unconfirmed
machine readings as commitments, not hiding an overdue contract inside a sorted
list, and not letting an empty calendar mean two opposite things at once.
"""

from __future__ import annotations

import pytest

from orpheus import api, bundle as bundle_mod, obligations, review

TODAY = "2026-09-01"


@pytest.fixture
def dated(store):
    """One document holding every case the calendar has to tell apart."""
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle)
    bundle_mod.apply_schema(store, bundle)
    store.insert("actors", {"actor_id": "act_r", "display_name": "R",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    store.insert("documents", {
        "document_id": "doc_1", "filename": "services.txt", "file_hash": "h1",
        "date_added": "2026-01-01T00:00:00Z", "created_by": "act_r",
        "visibility": "private", "review_status": "unreviewed"})
    store.insert("documents", {
        "document_id": "doc_2", "filename": "no-dates.txt", "file_hash": "h2",
        "date_added": "2026-01-01T00:00:00Z", "created_by": "act_r",
        "visibility": "private", "review_status": "unreviewed"})

    rows = [
        # (id, date, role, raw_text, status)
        ("kd_end",       "2026-11-15", "end",       "15 November 2026", "unconfirmed"),
        ("kd_overdue",   "2026-07-04", "end",       "04/07/2026",       "unconfirmed"),
        ("kd_milestone", "2026-09-30", "milestone", "2026-09-30",       "confirmed"),
        ("kd_start",     "2024-03-01", "start",     "1 March 2024",     "unconfirmed"),
        ("kd_signature", "2024-03-03", "signature", "3 March 2024",     "unconfirmed"),
        ("kd_unknown",   "2026-10-01", "unknown",   "1 October 2026",   "unconfirmed"),
        ("kd_rejected",  "2026-09-15", "end",       "15 September 2026", "rejected"),
        ("kd_far",       "2028-01-01", "end",       "1 January 2028",   "unconfirmed"),
    ]
    for n, (instance_id, value, role, raw, status) in enumerate(rows):
        store.insert("instances_KeyDate", {
            "instance_id": instance_id, "document_id": "doc_1", "value": value,
            "raw_text": raw, "date_role": role, "page_no": 1,
            "source": "ai_local", "confidence": 1.0, "status": status,
            "created_at": "2026-01-01T00:00:00Z"})
        store.insert("instance_index", {
            "instance_id": instance_id, "document_id": "doc_1",
            "type_id": "KeyDate", "table_name": "instances_KeyDate",
            "created_at": "2026-01-01T00:00:00Z"})
        store.insert("provenance", {
            "provenance_id": f"prov_{n}", "instance_id": instance_id,
            "document_id": "doc_1", "excerpt": raw, "page_no": 1,
            "source": "ai_local", "confidence": 1.0,
            "created_at": "2026-01-01T00:00:00Z"})
    return store


def _ids(entries):
    return [e["instance_id"] for e in entries]


# -- what it shows ------------------------------------------------------------

def test_it_shows_what_is_due_inside_the_window_and_nothing_beyond(dated):
    result = obligations.upcoming(dated, as_of=TODAY, within_days=90)
    assert _ids(result["due"]) == ["kd_milestone", "kd_end"]
    assert "kd_far" not in _ids(result["due"])
    assert result["window_ends"] == "2026-11-30"


def test_something_past_its_date_gets_its_own_section(dated):
    """Sorted into the list it would be one row among many, between things that
    have not happened yet. It is the most alarming row in the store."""
    result = obligations.upcoming(dated, as_of=TODAY)
    assert _ids(result["overdue"]) == ["kd_overdue"]
    assert result["overdue"][0]["days"] == -59
    assert _ids(result["due"]) == ["kd_milestone", "kd_end"]


def test_a_start_date_is_not_a_thing_that_falls_due(dated):
    """A contract *has* a start date and a signature date. Neither is something
    anybody has to act on, and a diary full of them is one nobody reads."""
    result = obligations.upcoming(dated, as_of=TODAY, within_days=3650)
    shown = set(_ids(result["due"]) + _ids(result["overdue"]))
    assert "kd_start" not in shown and "kd_signature" not in shown
    # A date with no role read from it is not a due date either -- but saying
    # so is not the same as dropping it silently.
    assert "kd_unknown" not in shown
    assert result["n_context_dates_set_aside"] == 3


def test_a_rejected_extraction_never_appears(dated):
    result = obligations.upcoming(dated, as_of=TODAY, within_days=3650)
    assert "kd_rejected" not in set(_ids(result["due"]) + _ids(result["overdue"]))


# -- what it refuses to imply -------------------------------------------------

def test_every_row_says_whether_a_person_checked_it(dated):
    result = obligations.upcoming(dated, as_of=TODAY)
    checked = {e["instance_id"]: e["reviewed"] for e in result["due"]}
    assert checked == {"kd_milestone": True, "kd_end": False}


def test_the_headline_splits_checked_from_unchecked(dated):
    """A diary of unconfirmed machine readings that looks like a diary is the
    single most damaging thing this module could produce, so the split is in
    the headline rather than in a column somebody has to notice."""
    result = obligations.upcoming(dated, as_of=TODAY)
    assert "1 of 3 shown have been checked by a person" in result["headline"]
    assert "machine readings nobody has confirmed" in result["headline"]


def test_a_calendar_of_confirmed_dates_says_so_instead(dated):
    for instance_id in ("kd_end", "kd_overdue"):
        review.confirm_instance(dated, instance_id, actor_id="act_r", note="ok")
    result = obligations.upcoming(dated, as_of=TODAY)
    assert "All 3 shown have been checked by a person" in result["headline"]


def test_a_date_that_reads_two_ways_says_what_else_it_could_be(dated):
    """"Ambiguous" on its own is a warning nobody can act on. `04/07/2026` is
    shown as 4 July because slash dates are read day-first; the useful thing to
    say beside it is "or 7 April", which is a different quarter."""
    result = obligations.upcoming(dated, as_of=TODAY)
    entry = result["overdue"][0]
    assert entry["ambiguous"] is True
    assert entry["other_reading"] == "2026-04-07"
    assert "can be read two ways" in result["headline"]


@pytest.mark.parametrize("raw,expected", [
    ("04/07/2026", "2026-04-07"),
    ("12.03.2022", "2022-12-03"),
    # Unambiguous: 15 cannot be a month.
    ("15/11/2026", None),
    # Same either way, so there is nothing to warn about.
    ("07/07/2026", None),
    ("15 November 2026", None),
    ("2026-11-15", None),
    (None, None),
])
def test_only_a_genuinely_two_way_date_is_flagged(raw, expected):
    assert obligations.other_reading(raw) == expected


# -- an empty calendar means two opposite things ------------------------------

def test_nothing_due_never_stands_on_its_own(dated):
    """Nothing due may mean nothing is due, or it may mean no end date was ever
    extracted. Those are opposite findings, and an entry list cannot tell them
    apart -- the same reason `graph.py` reports coverage beside its topology."""
    result = obligations.upcoming(dated, as_of=TODAY, document_id="doc_2")
    assert result["due"] == [] and result["overdue"] == []
    assert "Nothing falls due in this window" in result["headline"]
    assert "1 of 1 document(s) have no date that falls due at all" \
        in result["headline"]


def test_coverage_counts_the_documents_this_page_cannot_speak_for(dated):
    cover = obligations.upcoming(dated, as_of=TODAY)["coverage"]
    assert cover["n_documents"] == 2
    assert cover["n_with_a_due_date"] == 1
    assert cover["share"] == 0.5


def test_it_says_when_no_obligation_was_ever_extracted(dated):
    """Nothing in this codebase writes an `Obligation` -- only a model
    proposing one ever has. "No obligations" would otherwise read as "no
    obligations exist"."""
    note = obligations.upcoming(dated, as_of=TODAY)["coverage"]["note"]
    assert "No `Obligation` has been extracted" in note
    assert "The deterministic pass does not propose them" in note


def test_an_obligation_reports_its_recurrence_verbatim(dated):
    """"quarterly" and "annually on the anniversary" are free text a model
    wrote. Turning either into a series of dates would put entries in this
    calendar that no document contains."""
    dated.insert("instances_Obligation", {
        "instance_id": "ob_1", "document_id": "doc_1",
        "obligated_party": "Ardmore Digital Limited",
        "summary": "Submit a quarterly service report",
        "due_date": "2026-09-30", "recurrence": "quarterly",
        "source": "ai_local", "confidence": 0.9, "status": "unconfirmed",
        "created_at": "2026-01-01T00:00:00Z"})
    dated.insert("instance_index", {
        "instance_id": "ob_1", "document_id": "doc_1", "type_id": "Obligation",
        "table_name": "instances_Obligation",
        "created_at": "2026-01-01T00:00:00Z"})

    result = obligations.upcoming(dated, as_of=TODAY)
    entry = next(e for e in result["due"] if e["instance_id"] == "ob_1")
    assert entry["recurrence"] == "quarterly"
    assert entry["subject"] == "Ardmore Digital Limited"
    # One entry, not four. The next occurrence is what the document says; the
    # ones after it are what a parser would have guessed.
    assert len([e for e in result["due"] if e["type_id"] == "Obligation"]) == 1


# -- reproducibility ----------------------------------------------------------

def test_as_of_is_a_parameter_so_a_report_can_be_reproduced(dated):
    """"Seventeen things were due last quarter" is a claim about a date.
    Reading it off the clock makes the same command answer differently every
    morning with nothing recording why."""
    a = obligations.upcoming(dated, as_of="2026-07-01")
    b = obligations.upcoming(dated, as_of="2026-11-01")
    assert _ids(a["due"]) != _ids(b["due"])
    assert a["as_of"] == "2026-07-01" and b["as_of"] == "2026-11-01"
    assert obligations.upcoming(dated, as_of="2026-07-01")["due"] == a["due"]


# -- over the API -------------------------------------------------------------

def test_the_calendar_is_an_administrators_view(dated):
    dated.insert("actors", {"actor_id": "act_other", "display_name": "Bo",
                            "is_admin": 0, "created_at": "2026-01-01T00:00:00Z"})
    other = dict(dated.one("SELECT * FROM actors WHERE actor_id = 'act_other'"))
    status, payload = api.handle(dated, "GET", "/calendar", actor=other)
    assert status == 403
    assert "Pass `document_id`" in payload["error"]["message"]


def test_one_document_can_be_asked_by_anyone_who_may_read_it(dated):
    admin = dict(dated.one("SELECT * FROM actors WHERE actor_id = 'act_r'"))
    status, payload = api.handle(
        dated, "GET", "/calendar",
        body={"document_id": "doc_1", "as_of": TODAY}, actor=admin)
    assert status == 200
    assert _ids(payload["due"]) == ["kd_milestone", "kd_end"]
