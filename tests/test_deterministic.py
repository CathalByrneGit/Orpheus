from __future__ import annotations

import pytest

from orpheus.deterministic import (AMOUNT_ROLE_CUES, DATE_ROLE_CUES,
                                   find_all, find_amounts, find_dates,
                                   infer_role)
from orpheus.rubric import CONFIDENCE


def values(text):
    return {d["value"] for d in find_dates(text)}


# -- dates ------------------------------------------------------------------

def test_the_forms_contracts_actually_use():
    assert values("dated 2024-12-31") == {"2024-12-31"}
    assert values("dated 31 December 2024") == {"2024-12-31"}
    assert values("dated 31st December, 2024") == {"2024-12-31"}
    assert values("dated December 31, 2024") == {"2024-12-31"}
    assert values("dated 31/12/2024") == {"2024-12-31"}
    assert values("dated 5 Dec 2024") == {"2024-12-05"}


def test_a_slash_date_that_could_be_read_either_way_is_marked_down():
    # Day-first is right for Irish, UK and EU documents and wrong for US ones.
    # Rather than pick silently, an ambiguous one is recorded lower with its raw
    # text kept, so a reviewer can see what it came from.
    ambiguous = find_dates("dated 03/04/2024")[0]
    assert ambiguous["value"] == "2024-04-03"
    assert ambiguous["ambiguous"] is True
    assert ambiguous["confidence"] == CONFIDENCE["inferred"]

    unambiguous = find_dates("dated 31/12/2024")[0]
    assert unambiguous["ambiguous"] is False
    assert unambiguous["confidence"] == CONFIDENCE["explicit"]


@pytest.mark.parametrize("text", [
    "2026-02-31",          # ISO
    "31 February 2026",    # day-month-year
    "February 31, 2026",   # month-day-year
    "31/02/2026",          # slash
    "13/13/2026",          # no such month either way round
])
def test_a_date_that_does_not_exist_is_not_a_date(text):
    # The R implementation accepted all of these and stored them at the top of
    # the rubric: a nonexistent date recorded as explicit, which is the worst of
    # both -- wrong, and confident about it. Found by porting.
    assert find_dates(f"dated {text}") == []


def test_the_same_date_written_once_is_reported_once():
    assert len(find_dates("due 2024-12-31 and again 2024-12-31")) == 1


# -- amounts ----------------------------------------------------------------

def test_symbols_codes_and_words():
    def one(text):
        found = find_amounts(text)
        assert len(found) == 1, text
        return found[0]

    assert one("€2,400,000")["currency"] == "EUR"
    assert one("$1,500")["currency"] == "USD"
    assert one("£99.50")["amount"] == 99.5
    assert one("EUR 2,400,000")["amount"] == 2400000
    assert one("2,400,000 EUR")["currency"] == "EUR"


def test_the_euro_sign_is_found_whatever_the_locale():
    # In R this was a real failure and a subtle one: source must be ASCII, so
    # the symbol was written as an escape, which R marked UTF-8 while document
    # text read off disk was not. In a C-locale container they never matched and
    # no euro amount was ever found -- indistinguishable from a document with no
    # money in it.
    found = find_amounts("The total contract value is €1,480,000 exclusive of VAT.")
    assert found[0]["amount"] == 1480000
    assert found[0]["currency"] == "EUR"


def test_a_currency_read_from_a_word_scores_lower_than_one_read_from_a_code():
    from_code = find_amounts("EUR 500,000")[0]
    from_word = find_amounts("500,000 euro")[0]
    assert from_code["confidence"] == CONFIDENCE["explicit"]
    assert from_word["confidence"] == CONFIDENCE["named"]
    assert from_word["currency"] == "EUR"


# -- roles ------------------------------------------------------------------

def test_the_nearest_cue_wins_not_the_first_one_listed():
    text = "Commencing on 1 January 2024 and shall expire on 2026-12-31."
    roles = {d["value"]: d["role"] for d in find_all(text)}
    assert roles["2024-01-01"] == "start"
    assert roles["2026-12-31"] == "end"


@pytest.mark.parametrize("text,expected", [
    ("This Agreement terminates on 3 April 2027.", "end"),
    ("The Agreement terminating on 3 April 2027 shall not renew.", "end"),
    ("The licence expires on 3 April 2027.", "end"),
    ("The term commences on 3 April 2027.", "start"),
    ("Work commencing on 3 April 2027 is in scope.", "start"),
])
def test_cues_are_stems_so_ordinary_inflection_still_matches(text, expected):
    # Written as a property of the cue table rather than of one document: an
    # exact phrase added later passes every other test here and still misses
    # real contract language. "terminate on" did exactly that, and the nearest
    # surviving cue answered in its place -- storing a termination date as a
    # start date.
    position = text.index("3 April 2027")
    assert infer_role(text, position, DATE_ROLE_CUES) == expected


def test_a_finding_with_no_cue_nearby_is_left_unknown():
    assert infer_role("Reference 3 April 2027 appears here.",
                      len("Reference "), DATE_ROLE_CUES) == "unknown"


def test_amount_roles():
    text = ("The total contract value is EUR 1,000,000. "
            "Aggregate liability shall not exceed EUR 250,000. "
            "The day rate is EUR 800.")
    roles = {a["amount"]: a["role"] for a in find_amounts(text)
             for a in [dict(a, role=infer_role(text, a["position"], AMOUNT_ROLE_CUES))]}
    assert roles[1000000] == "contract_value"
    assert roles[250000] == "cap"
    assert roles[800] == "rate"


def test_find_all_carries_the_page_it_came_from():
    findings = find_all("Signed 2024-01-01 for EUR 10", page_no=3)
    assert findings and all(f["page_no"] == 3 for f in findings)
    assert {f["kind"] for f in findings} == {"date", "amount"}
