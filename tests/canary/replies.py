"""What the stub model says about each canary document.

Hand-written rather than generated, and every `excerpt` is copied verbatim from
the document it belongs to. That is deliberate: alignment is computed, so a
corpus edit that breaks a quote makes the canary fail loudly with the excerpt in
the message, rather than quietly extracting less.

Only the *model* is stubbed. Everything downstream of it -- `_post_chat`,
`_parse_chat_payload`, `normalise_population`, `align`, `insert_instance`,
`write_provenance`, the edge writer -- is the real code, which is where every
defect this session actually lived.
"""

from __future__ import annotations

CLASSIFY = {
    "01-ardmore-services.txt": {"doc_type": "contract", "sector": "health",
                                "jurisdiction": "Ireland", "confidence": 1.0},
    "02-ardmore-amendment.txt": {"doc_type": "amendment", "sector": "health",
                                 "jurisdiction": "Ireland", "confidence": 1.0},
    "03-kestrel-supply.txt": {"doc_type": "contract",
                              "sector": "local-government",
                              "jurisdiction": "Ireland", "confidence": 0.9},
    "04-halloran-framework.txt": {"doc_type": "contract", "sector": "transport",
                                  "jurisdiction": "Ireland", "confidence": 0.9},
    "05-halloran-calloff.txt": {"doc_type": "contract", "sector": "transport",
                                "jurisdiction": "Ireland", "confidence": 0.9},
    "06-tender-notice.txt": {"doc_type": "tender", "sector": "education",
                             "jurisdiction": "Ireland", "confidence": 1.0},
}

EXTRACT: dict[str, dict] = {
    "01-ardmore-services.txt": {
        "extractions": [
            {"instance_id": "c1", "type": "Contract",
             "excerpt": "SERVICES AGREEMENT",
             "properties": {"name": "Services Agreement",
                            "reference": "HSE/2024/0117",
                            "value_amount": "250000", "value_currency": "EUR",
                            "governing_law": "Ireland"}},
            {"instance_id": "s1", "type": "Company",
             "excerpt": "Ardmore Digital Limited",
             "properties": {"name": "Ardmore Digital Limited",
                            "registration_number": "482991",
                            "address": "12 Ushers Quay, Dublin 8",
                            "role": "supplier", "entity_kind": "company"}},
            # Wrapped across a line in the source, which is the ordinary case
            # in a real document and the one a naive `in` check would miss.
            {"instance_id": "b1", "type": "Company",
             "excerpt": "Health Service\nExecutive",
             "properties": {"name": "Health Service Executive",
                            "role": "buyer", "entity_kind": "public_body"}},
            {"instance_id": "p1", "type": "Person",
             "excerpt": "Nuala Ryan, Managing Director",
             "properties": {"name": "Nuala Ryan",
                            "job_title": "Managing Director",
                            "acting_for": "Ardmore Digital Limited"}},
        ],
        "relationships": [
            {"from_instance_id": "s1", "to_instance_id": "c1",
             "link_type_id": "party_to",
             "evidence": "between Ardmore Digital Limited"},
            {"from_instance_id": "b1", "to_instance_id": "c1",
             "link_type_id": "party_to",
             "evidence": "the Health Service\nExecutive (\"the Client\")"},
            {"from_instance_id": "p1", "to_instance_id": "s1",
             "link_type_id": "employed_by",
             "evidence": "Nuala Ryan, Managing Director"},
        ],
    },
    "02-ardmore-amendment.txt": {
        "extractions": [
            {"instance_id": "c1", "type": "Contract",
             "excerpt": "AMENDMENT NO. 1 TO THE SERVICES AGREEMENT",
             "properties": {"name": "Amendment No. 1 to the Services Agreement",
                            "reference": "HSE/2024/0117-A1",
                            "value_amount": "310000", "value_currency": "EUR"}},
            # The same company, spelled differently, with the same number. The
            # identifier is what should settle it, not the spelling.
            {"instance_id": "s1", "type": "Company",
             "excerpt": "Ardmore Digital Ltd",
             "properties": {"name": "Ardmore Digital Ltd",
                            "registration_number": "482991",
                            "role": "supplier", "entity_kind": "company"}},
            {"instance_id": "b1", "type": "Company",
             "excerpt": "the Health Service Executive",
             "properties": {"name": "Health Service Executive",
                            "role": "buyer", "entity_kind": "public_body"}},
            {"instance_id": "p1", "type": "Person",
             "excerpt": "Nuala Ryan, Managing Director",
             "properties": {"name": "Nuala Ryan",
                            "job_title": "Managing Director"}},
        ],
        "relationships": [
            {"from_instance_id": "s1", "to_instance_id": "c1",
             "link_type_id": "party_to",
             "evidence": "between Ardmore Digital Ltd"},
        ],
    },
    "03-kestrel-supply.txt": {
        "extractions": [
            {"instance_id": "c1", "type": "Contract",
             "excerpt": "SUPPLY AGREEMENT",
             "properties": {"name": "Supply Agreement",
                            "reference": "LGMA/2023/0042",
                            "value_amount": "88500", "value_currency": "EUR"}},
            {"instance_id": "s1", "type": "Company",
             "excerpt": "Kestrel Medical Group PLC",
             "properties": {"name": "Kestrel Medical Group PLC",
                            "registration_number": "551200",
                            "address": "3 Fitzwilliam Square, Dublin 2",
                            "role": "supplier", "entity_kind": "company"}},
            # A third spelling of a company that is a party elsewhere: this is
            # the subcontracting edge, and the page it lands on is the test.
            {"instance_id": "sub1", "type": "Company",
             "excerpt": "Ardmore Digital Limited",
             "properties": {"name": "Ardmore Digital Limited",
                            "role": "subcontractor", "entity_kind": "company"}},
            {"instance_id": "p1", "type": "Person",
             "excerpt": "Peter Halloran, Director, Kestrel Medical Group PLC",
             "properties": {"name": "Peter Halloran", "job_title": "Director"}},
        ],
        "relationships": [
            {"from_instance_id": "s1", "to_instance_id": "sub1",
             "link_type_id": "subcontracts_to",
             "evidence": "The Supplier may subcontract to Ardmore Digital Limited"},
            {"from_instance_id": "s1", "to_instance_id": "c1",
             "link_type_id": "party_to",
             "evidence": "between Kestrel Medical Group PLC"},
        ],
    },
    "04-halloran-framework.txt": {
        "extractions": [
            {"instance_id": "c1", "type": "Contract",
             "excerpt": "FRAMEWORK AGREEMENT",
             "properties": {"name": "Framework Agreement",
                            "reference": "OGP/2022/0311",
                            "value_amount": "2400000",
                            "value_currency": "EUR"}},
            {"instance_id": "s1", "type": "Company",
             "excerpt": "Halloran Instruments, Inc.",
             "properties": {"name": "Halloran Instruments, Inc.",
                            "registration_number": "771020",
                            "role": "supplier", "entity_kind": "company"}},
            {"instance_id": "p1", "type": "Person",
             "excerpt": "Peter Halloran, Chief Executive",
             "properties": {"name": "Peter Halloran",
                            "job_title": "Chief Executive"}},
        ],
        "relationships": [
            {"from_instance_id": "s1", "to_instance_id": "c1",
             "link_type_id": "party_to",
             "evidence": "between Halloran Instruments, Inc."},
            {"from_instance_id": "p1", "to_instance_id": "s1",
             "link_type_id": "employed_by",
             "evidence": "Peter Halloran, Chief Executive"},
        ],
    },
    "05-halloran-calloff.txt": {
        "extractions": [
            {"instance_id": "c1", "type": "Contract",
             "excerpt": "CALL-OFF CONTRACT UNDER FRAMEWORK OGP/2022/0311",
             "properties": {"name": "Call-off Contract",
                            "reference": "OGP/2022/0311-C7",
                            "value_amount": "145000",
                            "value_currency": "EUR"}},
            # No punctuation this time: the name key is what has to join these.
            {"instance_id": "s1", "type": "Company",
             "excerpt": "Halloran\nInstruments Inc",
             "properties": {"name": "Halloran Instruments Inc",
                            "role": "supplier", "entity_kind": "company"}},
            {"instance_id": "p1", "type": "Person",
             "excerpt": "Peter Halloran, Chief Executive",
             "properties": {"name": "Peter Halloran",
                            "job_title": "Chief Executive"}},
        ],
        "relationships": [
            {"from_instance_id": "s1", "to_instance_id": "c1",
             "link_type_id": "party_to",
             "evidence": "from Halloran"},
        ],
    },
    # A document with no parties at all. A corpus is not uniform, and a
    # pipeline that only works when every document is rich is not one.
    "06-tender-notice.txt": {
        "extractions": [
            {"instance_id": "c1", "type": "Contract",
             "excerpt": "INVITATION TO TENDER",
             "properties": {"name": "Invitation to Tender",
                            "reference": "DES/2025/0008",
                            "value_amount": "400000", "value_currency": "EUR",
                            "procurement_procedure": "open"}},
            {"instance_id": "b1", "type": "Company",
             "excerpt": "the Department of Education",
             "properties": {"name": "Department of Education",
                            "role": "buyer", "entity_kind": "public_body"}},
        ],
        "relationships": [],
    },
}
