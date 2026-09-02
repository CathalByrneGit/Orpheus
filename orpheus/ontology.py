"""Proposing an ontology for a corpus that does not have one yet.

Everything else in this store is written against a bundle. Ingest fills tables
the bundle declared, extraction fills columns it named, the wiki is built out of
types it listed, and the graph is a projection of links it defined. The claim
the project rests on is that none of that knows about contracts —
[the domain is the bundle](index.md), and swapping the bundle swaps the domain.

That claim has a hole in it, and this module is the hole. Swapping the bundle
assumes somebody has a bundle to swap in. For a domain nobody has modelled yet
the first question is not "what does this document say" but "what kinds of thing
are these documents about", and until it is answered there is no type to file an
answer under. So the first bundle has always been written by hand, by somebody
who had already read enough of the corpus to know what was in it — which is
exactly the work the rest of this project exists to help with.

**So: yes, the machine should help. No, it should not author.**

That is not a hedge, it is where the line actually falls, and the two halves
come from different observations.

A model reading forty documents is genuinely good at one thing here. It notices
that the same kind of thing keeps appearing, and that it keeps carrying the same
handful of attributes. That is a real reading of a corpus, it is tedious for a
person to do by hand across forty files, and it is the part that scales.

It is bad at the decision that follows. Whether `Author` and `Sponsor` are one
type with a role or two types. Whether `status` is a property of the proposal or
a thing in its own right with a history. Where the line between a person and the
office they hold runs. Those are not readings, they are commitments — and they
are the commitments that make an ontology somebody's rather than nobody's.

They are also expensive to get wrong in a way an extraction is not. A wrong
extraction is one row to amend; the machinery for that is
[already built](provenance-and-amendment.md). A wrong object type is every row
that will ever be written under it, a table, a wiki page kind, a set of edges,
and a migration for anyone who has already loaded data. `schema_ops.py` exists
because that turned out to be permanent once, and the cost of it is why.

So the split is the one this project keeps arriving at: **the machine proposes
with evidence, a person decides.** What a survey produces is a queue of
candidates — each with a quotation `align.py` located, and a count of how many
sampled documents show it — and a bundle only comes into being when somebody has
been through that queue. Nothing here writes a bundle as a side effect.

**Support is not confidence.** The model is never asked how sure it is, here as
anywhere else: `confidence` on a piece of evidence is what the alignment says,
by the same rubric everything else uses. What a reviewer wants instead is *how
many documents showed this*, out of how many were read — a type in one document
of forty is a different proposition from one in thirty-eight, and neither of
those is a probability.

**The cheap pass is the default and it is not a toy.** `deterministic` reads
`Key: Value` header blocks — the RFC 822 convention that mail, PEPs, Debian
control files, front matter and most memo templates all inherit — and proposes a
property per recurring key. It sends nothing anywhere, needs no opt-in, and
cannot propose a field the documents do not literally contain. On a corpus with
that shape it is not merely a fallback: it is *more* precise than a model,
because a header block is the corpus stating its own schema.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from .align import (MATCH_EXACT, MATCH_GREATER, MATCH_LESSER,
                    align)
from .audit import record_edit
from .ingest import document_text
from .population import confidence_for_alignment, page_offsets
from .rubric import CANDIDATE_KINDS, CANDIDATE_STATUSES, RESERVED_PROPS
from .store import Store
from .utils import (NotFound, OrpheusError, NAME_STYLES, new_id, now,
                    require_choice, require_string)

#: No model, no network, no gate. See the module docstring on why this leads.
DEFAULT_ENGINE = "deterministic"

#: How many documents a survey reads. An ontology is a claim about a corpus and
#: reading four of forty documents makes a claim about four; reading all forty
#: through a model is forty calls to answer a question that is usually settled
#: well before that. Twenty is the compromise, and it is reported alongside
#: every candidate so nobody has to guess what the support is out of.
DEFAULT_SAMPLE = 20

#: A pattern cannot be read off a single observation. The same sentence governs
#: the enrichment's batch rule, and it means the same thing here: one document
#: showing a field is one document, and a survey that reports it as a finding
#: gives a reviewer a queue of every stray line in the corpus.
DEFAULT_MIN_SUPPORT = 2

#: Quotations kept per candidate. Enough to see it is not a one-off, few enough
#: that a reviewer reads them. The count that says how widespread it is lives in
#: `n_documents`, so this does not have to carry that job as well.
MAX_EVIDENCE = 3

#: How much of each document a model is shown. Charged to the same character
#: budget as everything else -- twenty documents at their full length is a large
#: call to answer a question the first pages usually settle, because a document
#: that declares its own structure declares it at the top.
DEFAULT_CHARS_PER_DOCUMENT = 6000

#: The type a header-block survey hangs its fields on when the caller does not
#: name one. Deliberately colourless: the deterministic pass has found fields,
#: not a thing, and a name it invented would be the one part of its output that
#: was not read off the page. Renaming it is the ordinary first review.
DEFAULT_PRIMARY_TYPE = "Record"


# ---------------------------------------------------------------------------
# The header-block pass
# ---------------------------------------------------------------------------

#: `Key: Value` at the start of a line. The key is bounded because a colon in
#: prose is not a field name and "As the court held in Smith v Jones:" would
#: otherwise be one.
#:
#: The value may be empty. A field the corpus declares and leaves blank is still
#: a field the corpus has -- ten of forty documents in the calibration corpus
#: carry a bare `Post-History:`, and requiring a value counted the field in
#: twenty-four of them instead of thirty-four. What stops a bulleted list of
#: bare `Something:` lines becoming an ontology is the block rule below.
_FIELD = re.compile(r"^([A-Z][A-Za-z0-9][A-Za-z0-9 _/-]{0,38}):[ \t]*(.*)$")

#: A folded continuation, RFC 822 style: an indented line belongs to the field
#: above it. Recognised so that a wrapped value does not end the block.
_FOLD = re.compile(r"^[ \t]+\S")

#: How many fields in a row make a header block. Two adjacent lines with colons
#: happen in prose all the time; three is where it stops being a coincidence,
#: and requiring it is what keeps this pass from proposing `Note` and `Warning`
#: as properties of everything.
MIN_BLOCK = 3


def header_fields(text: str) -> list[dict]:
    """The `Key: Value` fields of every header block in one document.

    A *block*, not a line. The convention this reads is a run of fields with
    nothing between them, and the run is what tells a field apart from a
    sentence that happens to contain a colon. Anything shorter than
    `MIN_BLOCK` is dropped rather than reported at low confidence: it is not
    weak evidence of a field, it is evidence of prose.

    Positions are into `text` as given, so a caller can locate what it found.
    """
    lines = text.splitlines(keepends=True)
    offsets, cursor = [], 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)

    found: list[dict] = []
    block: list[dict] = []

    def flush():
        # Long enough to be a block, and carrying at least one value. Three
        # lines that all end in a bare colon are a list of headings, and a
        # survey that read them as fields would propose a property per section
        # of every structured document it saw.
        if len(block) >= MIN_BLOCK and any(f["value"] for f in block):
            found.extend(block)
        block.clear()

    for index, line in enumerate(lines):
        match = _FIELD.match(line.rstrip("\n"))
        if match:
            key, value = match.group(1).strip(), match.group(2).strip()
            block.append({
                "key": key, "value": value,
                "position": offsets[index],
                "raw_text": line.rstrip("\n"),
            })
            continue
        if block and _FOLD.match(line):
            # A wrapped value. It belongs to the field above and does not end
            # the block -- ending it here would split one header in two and
            # then discard both halves for being too short.
            block[-1]["value"] += " " + line.strip()
            continue
        flush()
    flush()
    return found


def property_id_for(key: str) -> str:
    """A column name for a field a document called something human.

    Lowercased and underscored, which is what every other property id in every
    bundle looks like. Nothing clever: a reviewer renames it if it is wrong, and
    a transformation they cannot predict is worse than one they can.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    return slug or "field"


_INTEGER = re.compile(r"^-?\d{1,12}$")
_NUMBER = re.compile(r"^-?\d{1,12}(\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}$")


def infer_data_type(values: list[str]) -> str:
    """The narrowest type every observed value fits.

    Every value, not most: a column typed `integer` because nine of ten values
    were numbers is a column that cannot hold the tenth, and the tenth is
    usually the interesting one. A single disagreeing value makes it a string,
    which is what the bundle would have said anyway.
    """
    seen = [v.strip() for v in values if v and v.strip()]
    if not seen:
        return "string"
    if all(_INTEGER.match(v) for v in seen):
        return "integer"
    if all(_NUMBER.match(v) for v in seen):
        return "number"
    if all(_DATE.match(v) for v in seen):
        return "date"
    return "string"


# ---------------------------------------------------------------------------
# Surveying
# ---------------------------------------------------------------------------

def _sample(store: Store, sample: int,
            document_ids: list[str] | None) -> list[dict]:
    if document_ids:
        rows = [store.one("SELECT document_id, filename FROM documents "
                          "WHERE document_id = ?", (d,))
                for d in document_ids]
        missing = [d for d, row in zip(document_ids, rows) if row is None]
        if missing:
            raise NotFound(f"No document {missing[0]!r}.")
        return [r for r in rows if r]
    # Oldest first, deliberately. A survey re-run after more documents arrive
    # should read the same documents plus the new ones, not a different corpus
    # that happens to be the same size -- otherwise the support counts move for
    # reasons nobody can see.
    return store.query(
        "SELECT document_id, filename FROM documents "
        "ORDER BY date_added, document_id LIMIT ?", (max(1, int(sample)),))


def survey(store: Store, engine: str = DEFAULT_ENGINE,
           sample: int = DEFAULT_SAMPLE, actor_id: str | None = None,
           tier: str = "local", opt_in: bool = False,
           min_support: int = DEFAULT_MIN_SUPPORT,
           document_ids: list[str] | None = None,
           primary_type: str = DEFAULT_PRIMARY_TYPE,
           chars_per_document: int = DEFAULT_CHARS_PER_DOCUMENT) -> dict:
    """Read a sample of the corpus and propose what it seems to be about.

    Writes candidates, never a bundle. Re-runnable: a shape proposed again is
    the same candidate with more evidence behind it, and a shape somebody has
    already decided about is not offered a second time.
    """
    store.assert_writable()
    require_choice(engine, tuple(_engines()), "engine")
    documents = _sample(store, sample, document_ids)
    if not documents:
        raise OrpheusError(
            "There are no documents to survey. An ontology is a claim about a "
            "corpus, and this store has no corpus yet.")

    survey_id = new_id("srv")
    if engine == DEFAULT_ENGINE:
        found = _header_survey(store, documents, primary_type)
    else:
        found = _engine_survey(store, documents, engine, tier, opt_in,
                               actor_id, chars_per_document)

    n_sampled = len(documents)
    kept: list[str] = []
    held_back: list[dict] = []
    with store.transaction():
        for shape, proposal in found.items():
            if len(proposal["documents"]) < max(1, int(min_support)):
                # Named, not merely counted. On a corpus with no header block
                # to corroborate against, support is quotation support and
                # everything sits low -- so "5 were held back" tells a reviewer
                # a threshold is doing work and not what it did. These are the
                # shapes they would see by lowering it.
                held_back.append({
                    "kind": shape[0], "type_id": shape[1],
                    "property_id": shape[2], "to_type_id": shape[3],
                    "n_documents": len(proposal["documents"]),
                })
                continue
            candidate_id = _record(store, survey_id, shape, proposal,
                                   n_sampled, engine, tier, actor_id)
            if candidate_id:
                kept.append(candidate_id)

    return {
        "survey_id": survey_id,
        "engine": engine,
        "n_documents_read": n_sampled,
        "documents": [d["document_id"] for d in documents],
        "n_candidates": len(kept),
        # Reported rather than hidden. A survey that proposed forty shapes and
        # kept four is telling a reviewer that the threshold is doing the work,
        # and that lowering it is a choice available to them.
        "n_below_support": len(held_back),
        "below_support": sorted(held_back, key=lambda h: -h["n_documents"]),
        "min_support": min_support,
        "candidates": [get_candidate(store, c) for c in kept],
    }


def _engines() -> list[str]:
    from .engines import engine_names
    return [DEFAULT_ENGINE] + list(engine_names())


def _record(store: Store, survey_id: str, shape: tuple, proposal: dict,
            n_sampled: int, engine: str, tier: str,
            actor_id: str | None) -> str | None:
    """Insert a candidate, or add evidence to the one already there.

    A shape somebody has already decided about is left alone. Re-proposing a
    rejected type on every survey is how a queue stops being read, and the
    decision is the more recent information anyway.
    """
    kind, type_id, property_id, to_type_id = shape
    existing = store.one(
        "SELECT candidate_id, status, n_documents FROM ontology_candidates "
        "WHERE kind = ? AND type_id = ? AND IFNULL(property_id, '') = ? "
        "AND IFNULL(to_type_id, '') = ?",
        (kind, type_id, property_id or "", to_type_id or ""))

    if existing:
        if existing["status"] != "proposed":
            return None
        candidate_id = existing["candidate_id"]
        store.execute(
            "UPDATE ontology_candidates SET n_documents = ?, n_sampled = ?, "
            "survey_id = ? WHERE candidate_id = ?",
            (max(existing["n_documents"], len(proposal["documents"])),
             n_sampled, survey_id, candidate_id))
    else:
        candidate_id = new_id("cnd")
        store.insert("ontology_candidates", {
            "candidate_id": candidate_id,
            "survey_id": survey_id,
            "kind": kind,
            "type_id": type_id,
            "property_id": property_id,
            "to_type_id": to_type_id,
            "data_type": proposal.get("data_type"),
            "name_style": proposal.get("name_style"),
            "display_name": proposal.get("display_name"),
            "description": proposal.get("description"),
            "rationale": proposal.get("rationale"),
            "n_documents": len(proposal["documents"]),
            "n_sampled": n_sampled,
            "engine": engine,
            "source": "ai_cloud" if tier == "cloud" else "ai_local",
            "status": "proposed",
            "accepted_as": None,
            "created_at": now(),
            "decided_by": None,
            "decided_at": None,
            "note": None,
        })

    # Already-held quotations, so a second survey adds what it found rather
    # than repeating what the first one did. Two runs quoting the same line of
    # the same document is one piece of evidence, and showing it twice makes a
    # candidate look better supported than it is.
    held = {(row["document_id"], row["excerpt"]) for row in store.query(
        "SELECT document_id, excerpt FROM ontology_evidence "
        "WHERE candidate_id = ?", (candidate_id,))}
    for item in proposal["evidence"]:
        if len(held) >= MAX_EVIDENCE:
            break
        key = (item["document_id"], item["excerpt"])
        if key in held:
            continue
        held.add(key)
        store.insert("ontology_evidence", {
            "evidence_id": new_id("evd"),
            "candidate_id": candidate_id,
            "document_id": item["document_id"],
            "page_no": item.get("page_no"),
            "excerpt": item["excerpt"],
            "char_start": item.get("char_start"),
            "char_end": item.get("char_end"),
            "alignment": item.get("alignment"),
            "confidence": item.get("confidence"),
        })
    return candidate_id


def _header_survey(store: Store, documents: list[dict],
                   primary_type: str) -> dict:
    """Fields, from the corpus stating its own schema.

    Grounded by construction, like the companion's pattern pass: every key
    proposed is a run of characters at the start of a line in a document, so
    there is nothing here for `align.py` to fail to find.

    It proposes one object type and hangs the fields on it. That is the honest
    limit of what a header block says: these documents have fields, and they
    are fields *of the document*. Whether two of those fields are really
    attributes of a person who should be a type of their own is exactly the
    question this pass cannot answer and a reviewer can.
    """
    values: dict[str, list[str]] = defaultdict(list)
    seen_in: dict[str, set[str]] = defaultdict(set)
    evidence: dict[str, list[dict]] = defaultdict(list)
    display: dict[str, str] = {}
    type_evidence: list[dict] = []
    type_documents: set[str] = set()

    for document in documents:
        document_id = document["document_id"]
        text = document_text(store, document_id)
        pages = page_offsets(store, document_id)
        fields = header_fields(text)
        if not fields:
            continue
        type_documents.add(document_id)
        first = fields[0]
        type_evidence.append(_evidence(document_id, text, first["raw_text"],
                                       pages, hint=first["position"]))
        for field in fields:
            property_id = property_id_for(field["key"])
            if property_id in RESERVED_PROPS:
                # The store owns this column. A corpus whose documents carry a
                # `Status:` header is common and the collision is real, so it is
                # skipped here rather than caught by bundle validation later.
                continue
            display.setdefault(property_id, field["key"])
            values[property_id].append(field["value"])
            seen_in[property_id].add(document_id)
            if len(evidence[property_id]) < MAX_EVIDENCE:
                evidence[property_id].append(
                    _evidence(document_id, text, field["raw_text"], pages,
                              hint=field["position"]))

    found: dict[tuple, dict] = {}
    if type_documents:
        found[("object_type", primary_type, None, None)] = {
            "documents": type_documents,
            "evidence": type_evidence[:MAX_EVIDENCE],
            "display_name": primary_type,
            "description": ("One document of this corpus, and the fields its "
                            "header block declares."),
            "rationale": (f"{len(type_documents)} of the documents read open "
                          "with a header block, so the corpus states its own "
                          "fields. What the thing they describe should be "
                          "called is not something a header block says."),
        }
    for property_id, documents_seen in seen_in.items():
        found[("property", primary_type, property_id, None)] = {
            "documents": documents_seen,
            "evidence": evidence[property_id],
            "data_type": infer_data_type(values[property_id]),
            "display_name": display[property_id],
            "description": f"Read from a '{display[property_id]}:' header.",
            "rationale": (f"Present as a header field in "
                          f"{len(documents_seen)} of the documents read."),
        }
    return found


def _evidence(document_id: str, text: str, quote: str,
              pages: list[tuple[int, int, int]],
              hint: int | None = None) -> dict:
    """One quotation, located in the document it claims to come from.

    Computed, not trusted -- the same rule as everywhere else. A model's claim
    to have quoted the corpus is not evidence that it did, and a candidate whose
    only support is a sentence nobody can find in the corpus is worse than no
    candidate: a reviewer accepting it is accepting the machine's word.
    """
    start, end, alignment = align(text, quote, hint)
    page_no = None
    if start is not None:
        for number, begin, finish in pages:
            if begin <= start < finish:
                page_no = number
                break
    return {
        "document_id": document_id,
        "page_no": page_no,
        "excerpt": quote,
        "char_start": start,
        "char_end": end,
        "alignment": alignment,
        "confidence": confidence_for_alignment(alignment),
    }


# ---------------------------------------------------------------------------
# The model pass
# ---------------------------------------------------------------------------

SURVEY_SYSTEM = """\
You are helping someone decide what kinds of thing a set of documents is about,
so that a database can be built to hold them. You are not extracting facts.

You will be shown the opening of several documents from one collection. Propose
the object types the collection is about, and for each one the properties it
carries. Propose only what recurs: a type that appears in one document is a
detail of that document, not a kind of thing the collection is about.

Return JSON only, of the form:
{"types": [
  {"id": "PascalCaseTypeName",
   "display_name": "Human readable name",
   "description": "One sentence on what one of these is.",
   "name_style": "organisation" | "personal" | null,
   "rationale": "Why you think this is a kind of thing rather than a property.",
   "quotes": ["verbatim text from a document that shows one"],
   "properties": [
     {"id": "snake_case_name", "display_name": "Human readable",
      "type": "string" | "number" | "integer" | "boolean" | "date",
      "description": "What this holds.",
      "quotes": ["verbatim text showing this property's value"]}
   ]}
 ],
 "links": [
  {"id": "snake_case_relation", "from": "TypeName", "to": "TypeName",
   "display_name": "Human readable", "description": "What relates them.",
   "quotes": ["verbatim text showing the relationship"]}
 ]}

Rules:
- Every quote must be copied character for character from a document shown. A
  quote that is not in the text is worse than no quote: it is the only thing the
  person reviewing your proposal has to check it against.
- `name_style` says how this type's names normalise, and applies only to types
  with a name: "personal" for people, "organisation" for bodies and titled
  things, null for anything else.
- Do not propose `instance_id`, `document_id`, `source`, `confidence`,
  `status`, `created_at`, `amended_by` or `amended_at`. Those belong to the
  platform, not to the domain.
- Return no prose, no explanation and no code fence.
"""


def _engine_survey(store: Store, documents: list[dict], engine: str,
                   tier: str, opt_in: bool, actor_id: str | None,
                   chars_per_document: int) -> dict:
    """One call, over the openings of every sampled document.

    One call and not one per document, which is the opposite of how extraction
    works, because the question is different. "What does this document say" is
    answerable a document at a time; "what kinds of thing recur across these
    documents" is not answerable at all from one document, and asking it twenty
    times would produce twenty ontologies to reconcile — which is the hard part
    of the job, done twenty times worse.

    The openings rather than the whole documents: a corpus that declares its own
    structure declares it at the top, and twenty documents at full length is a
    call large enough that a person would want to have been asked first.
    """
    from .engines import ask

    texts: dict[str, str] = {}
    parts = []
    for index, document in enumerate(documents, start=1):
        text = document_text(store, document["document_id"])
        texts[document["document_id"]] = text
        parts.append(f"--- Document {index}: {document['filename']} ---\n"
                     + text[:max(0, int(chars_per_document))])
    reply = ask(store=store, system=SURVEY_SYSTEM, text="\n\n".join(parts),
                purpose="survey", engine=engine, tier=tier, opt_in=opt_in,
                actor_id=actor_id)

    proposed = _parse_survey(reply)
    found: dict[tuple, dict] = {}
    for entry in proposed.get("types") or []:
        type_id = _identifier(entry.get("id"), style="type")
        if not type_id:
            continue
        found[("object_type", type_id, None, None)] = {
            "documents": _documents_quoting(entry.get("quotes"), texts),
            "evidence": _evidence_for(entry.get("quotes"), texts),
            "display_name": entry.get("display_name") or type_id,
            "description": entry.get("description"),
            "rationale": entry.get("rationale"),
            "name_style": (entry.get("name_style")
                           if entry.get("name_style") in NAME_STYLES else None),
        }
        for prop in entry.get("properties") or []:
            property_id = _identifier(prop.get("id"), style="property")
            if not property_id or property_id in RESERVED_PROPS:
                continue
            found[("property", type_id, property_id, None)] = {
                "documents": _documents_quoting(prop.get("quotes"), texts),
                "evidence": _evidence_for(prop.get("quotes"), texts),
                "data_type": _data_type(prop.get("type")),
                "display_name": prop.get("display_name") or property_id,
                "description": prop.get("description"),
                "rationale": None,
            }

    _corroborate(found, texts)

    for link in proposed.get("links") or []:
        link_id = _identifier(link.get("id"), style="property")
        from_id = _identifier(link.get("from"), style="type")
        to_id = _identifier(link.get("to"), style="type")
        if not (link_id and from_id and to_id):
            continue
        found[("link_type", from_id, link_id, to_id)] = {
            "documents": _documents_quoting(link.get("quotes"), texts),
            "evidence": _evidence_for(link.get("quotes"), texts),
            "display_name": link.get("display_name") or link_id,
            "description": link.get("description"),
            "rationale": None,
        }
    return found


def _corroborate(found: dict, texts: dict[str, str]) -> None:
    """Let the cheap pass measure the extent of what the expensive one proposed.

    A model's support count is a count of *its quotations*: it proposed
    `title`, cited two documents, and the survey reported 2 of 40 for a field
    every document in the corpus carries. The number is not wrong -- it is
    checked, and the checking is what makes it worth anything -- but read as
    "how much of the corpus has this" it is a floor and a misleading one, and a
    reviewer sorting the queue by support would find the real types at the
    bottom of it.

    So where the corpus states its own fields, the header pass counts them. A
    property is present in a document if that document's header block carries a
    key that normalises to the same id. Nothing is invented: a document with no
    header block contributes nothing, and a property the model named something
    the documents do not call it stays at its quotation count -- which is
    itself the signal that a reviewer should rename it.

    Object types and links are left alone. A header block says a field is
    there; it says nothing about how many documents are about a Person.
    """
    fields: dict[str, set[str]] = defaultdict(set)
    for document_id, text in texts.items():
        for field in header_fields(text):
            fields[property_id_for(field["key"])].add(document_id)
    for shape, proposal in found.items():
        kind, _type_id, property_id, _to = shape
        if kind == "property" and property_id in fields:
            proposal["documents"] = set(proposal["documents"]) \
                | fields[property_id]

    # A type is supported at least as well as its best-supported property. You
    # cannot have the title of a proposal in forty documents and the proposal
    # itself in three: the property is an attribute *of* the thing, so finding
    # the attribute is finding the thing.
    #
    # Without this a model that quoted its types sparsely and its properties
    # widely left a queue with properties above the support threshold and no
    # type for them to be properties of -- which is not a modelling question a
    # reviewer can answer, it is an artefact of how many quotations the model
    # happened to give.
    for shape, proposal in found.items():
        if shape[0] != "object_type":
            continue
        for other_shape, other in found.items():
            if other_shape[0] == "property" and other_shape[1] == shape[1]:
                proposal["documents"] = set(proposal["documents"]) \
                    | set(other["documents"])


def _parse_survey(reply: str) -> dict:
    from .engines import _FENCE

    text = (reply or "").strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    import json
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise OrpheusError(
            f"The survey reply was not JSON, so there is nothing to review: "
            f"{exc}. The first 200 characters were: {text[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise OrpheusError("The survey reply was not an object.")
    return parsed


_DATA_TYPES = ("string", "number", "integer", "boolean", "date")


def _data_type(name) -> str:
    value = str(name or "").strip().lower()
    return value if value in _DATA_TYPES else "string"


def _identifier(raw, style: str) -> str | None:
    """A safe id, or None.

    Bundle ids become table and column names, so a model returning
    `"Person (party)"` must not turn into a quoted identifier nobody can type.
    Sanitised rather than rejected, because the proposal is still worth
    reviewing and the reviewer can rename it.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if style == "type":
        parts = re.split(r"[^A-Za-z0-9]+", text)
        out = "".join(p[:1].upper() + p[1:] for p in parts if p)
        return out or None
    return property_id_for(text) or None


def _documents_quoting(quotes, texts: dict[str, str]) -> set[str]:
    """Which documents actually contain one of these quotations.

    Support is measured here rather than taken from the model, and this is the
    number a reviewer leans on hardest. A model asked to propose types from
    twenty documents will say a type recurs; whether its quotations are found in
    one document or in fourteen is checkable, and is checked.
    """
    seen = set()
    for quote in _clean_quotes(quotes):
        for document_id, text in texts.items():
            _, _, alignment = align(text, quote)
            if alignment in COUNTS_AS_PRESENT:
                seen.add(document_id)
    return seen


#: Alignments that mean this document contains this quotation.
#:
#: Everything except `match_fuzzy`, and the exception is the point. Locating an
#: excerpt in the document it came from, a fuzzy match is a real answer -- the
#: model rewrapped a line, and the passage is still there to be read. Counting
#: how many documents contain a quotation it is not: `align` falls back to
#: matching three consecutive words, and a proposed `abstract` property quoting
#: "This PEP proposes a redesign and re-implementation of..." was found in
#: twenty-one of forty documents that share nothing but the opening phrase.
#: Support has to mean "this is here", and fuzzy means "something like this
#: is".
COUNTS_AS_PRESENT = (MATCH_EXACT, MATCH_GREATER, MATCH_LESSER)


def _clean_quotes(quotes) -> list[str]:
    if isinstance(quotes, str):
        quotes = [quotes]
    out = []
    for quote in quotes or []:
        text = str(quote or "").strip()
        if text:
            out.append(text)
    return out


def _evidence_for(quotes, texts: dict[str, str]) -> list[dict]:
    """Every quotation, located in whichever document holds it.

    A quotation found nowhere is dropped rather than stored at low confidence.
    Elsewhere an unlocatable excerpt still has a property value attached to it
    and is worth keeping at `inferred`; here the quotation *is* the whole of the
    evidence, and a candidate whose support is a sentence that exists in no
    document is a candidate a reviewer would be accepting on trust.
    """
    out = []
    for quote in _clean_quotes(quotes):
        for document_id, text in texts.items():
            start, end, alignment = align(text, quote)
            if start is None:
                continue
            out.append({
                "document_id": document_id, "page_no": None,
                "excerpt": quote, "char_start": start, "char_end": end,
                "alignment": alignment,
                "confidence": confidence_for_alignment(alignment),
            })
            break
    return out[:MAX_EVIDENCE]


# ---------------------------------------------------------------------------
# Reading the queue
# ---------------------------------------------------------------------------

def get_candidate(store: Store, candidate_id: str) -> dict:
    row = store.one("SELECT * FROM ontology_candidates WHERE candidate_id = ?",
                    (candidate_id,))
    if row is None:
        raise NotFound(f"No ontology candidate {candidate_id!r}.")
    row["evidence"] = evidence_for(store, candidate_id)
    return row


def evidence_for(store: Store, candidate_id: str) -> list[dict]:
    return store.query(
        "SELECT e.*, d.filename FROM ontology_evidence e "
        "JOIN documents d ON d.document_id = e.document_id "
        "WHERE e.candidate_id = ? ORDER BY e.rowid", (candidate_id,))


#: How many candidates a queue hands over at once.
#:
#: A queue is not a listing. You decide the top item, it leaves the queue, and
#: the next one appears -- so the useful thing is the front of it plus a count
#: of what is behind, and paging through it fights the workflow: decide the
#: third item on page two and everything shifts under you. Every candidate
#: carries its excerpts, so an uncapped queue is also a screen nobody can read
#: and a query per row for evidence nobody will look at.
QUEUE_LIMIT = 25


def _candidate_filter(status: str, kind: str | None) -> tuple[str, list]:
    where, params = "WHERE status = ?", [status]
    if kind:
        require_choice(kind, CANDIDATE_KINDS, "kind")
        where += " AND kind = ?"
        params.append(kind)
    return where, params


def n_candidates(store: Store, status: str = "proposed",
                 kind: str | None = None) -> int:
    """How many are in the queue, whatever the page is showing."""
    where, params = _candidate_filter(status, kind)
    return store.scalar(
        f"SELECT COUNT(*) FROM ontology_candidates {where}", tuple(params))


def candidates(store: Store, status: str = "proposed",
               kind: str | None = None,
               limit: int | None = QUEUE_LIMIT) -> list[dict]:
    """The queue, most-supported first.

    Ordered by how many documents show it, because that is the order somebody
    reviewing an ontology wants: the types the corpus is really about come
    first, and the tail is where the doubtful ones are. Which is also why a cap
    costs nothing: it takes the front of that order, and `n_candidates` says
    what is behind it.

    `limit=None` for the whole queue, which is what `draft` needs and a screen
    does not.
    """
    where, params = _candidate_filter(status, kind)
    sql = (f"SELECT * FROM ontology_candidates {where} "
           "ORDER BY n_documents DESC, kind, type_id, IFNULL(property_id, '')")
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = store.query(sql, tuple(params))
    for row in rows:
        row["evidence"] = evidence_for(store, row["candidate_id"])
    return rows


def review_candidate(store: Store, candidate_id: str, decision: str,
                     actor_id: str, accepted_as: str | None = None,
                     note: str | None = None) -> dict:
    """Accept, rename or reject one proposal.

    `accepted_as` is the one that matters. A survey is good at noticing that
    something recurs and bad at naming it, so the ordinary accepting move is
    "yes, and it is called this" -- and recording that as a rejection followed
    by a hand-written type would throw away the evidence that argued for it.
    Renaming keeps the quotations attached to the thing they were quotations
    for.
    """
    store.assert_writable()
    require_string(actor_id, "actor_id")
    require_choice(decision, ("accepted", "rejected"), "decision")
    candidate = store.one(
        "SELECT * FROM ontology_candidates WHERE candidate_id = ?",
        (candidate_id,))
    if candidate is None:
        raise NotFound(f"No ontology candidate {candidate_id!r}.")
    if candidate["status"] != "proposed":
        raise OrpheusError(
            f"Candidate {candidate_id} was already {candidate['status']}.")

    renamed = None
    if decision == "accepted" and accepted_as:
        style = "type" if candidate["kind"] == "object_type" else "property"
        renamed = _identifier(accepted_as, style=style)
        if not renamed:
            raise OrpheusError(
                f"{accepted_as!r} does not reduce to a usable identifier.")
        if style == "property" and renamed in RESERVED_PROPS:
            raise OrpheusError(
                f"{renamed!r} is a name the store reserves for provenance, so "
                "a domain property cannot take it.")
    status = "amended" if renamed else decision

    with store.transaction():
        store.execute(
            "UPDATE ontology_candidates SET status = ?, accepted_as = ?, "
            "decided_by = ?, decided_at = ?, note = ? WHERE candidate_id = ?",
            (status, renamed, actor_id, now(), note, candidate_id))
        record_edit(store, "ontology_candidates", candidate_id, None,
                    f"ontology_candidate_{status}",
                    previous={"status": "proposed"},
                    new={"status": status, "accepted_as": renamed},
                    actor_id=actor_id, note=note)
    return get_candidate(store, candidate_id)


def reopen_candidate(store: Store, candidate_id: str, actor_id: str,
                     note: str | None = None) -> dict:
    """Put a decided candidate back in the queue.

    Some consequences of a decision are only visible after the extraction has
    run. Accepting `Meeting.date` rather than accepting it as `name` is a
    reasonable call that reads as obviously right until the graph comes back at
    8% coverage, because a type with no `name` gets no wiki page and every edge
    through it has nowhere to land. `draft_bundle` warns about that now, and a
    warning is only worth having if the decision it warns about can be changed.

    The evidence stays attached and the change is recorded, so the queue shows
    a candidate somebody thought about twice rather than one nobody decided.
    Reopening is not undoing: what it restores is the question, not the state
    before it was asked.

    It does *not* touch a bundle already registered from the old decision. A
    drafted bundle is a file somebody chose to install, and rewriting an
    ontology under rows already filed against it is `schema_ops.py`'s job, on
    purpose.
    """
    store.assert_writable()
    require_string(actor_id, "actor_id")
    candidate = store.one(
        "SELECT * FROM ontology_candidates WHERE candidate_id = ?",
        (candidate_id,))
    if candidate is None:
        raise NotFound(f"No ontology candidate {candidate_id!r}.")
    if candidate["status"] == "proposed":
        raise OrpheusError(
            f"Candidate {candidate_id} is already in the queue.")

    was = candidate["status"]
    with store.transaction():
        store.execute(
            "UPDATE ontology_candidates SET status = 'proposed', "
            "accepted_as = NULL, decided_by = NULL, decided_at = NULL, "
            "note = ? WHERE candidate_id = ?", (note, candidate_id))
        record_edit(store, "ontology_candidates", candidate_id, None,
                    "ontology_candidate_reopened",
                    previous={"status": was,
                              "accepted_as": candidate["accepted_as"]},
                    new={"status": "proposed"}, actor_id=actor_id, note=note)
    return get_candidate(store, candidate_id)


# ---------------------------------------------------------------------------
# Turning what was accepted into a bundle
# ---------------------------------------------------------------------------

#: The columns the store writes on every managed instance table. Declared here
#: because a bundle assembled from candidates has to carry them and a person
#: reviewing candidates was never asked about them: they are not domain
#: knowledge, they are how a row records where it came from and who checked it.
PROVENANCE_PROPERTIES = [
    {"id": "document_id", "type": "string", "nullable": True,
     "source": {"column": "document_id"}},
    {"id": "source", "type": "string", "nullable": True,
     "source": {"column": "source"}},
    {"id": "confidence", "type": "number", "nullable": True,
     "source": {"column": "confidence"}},
    {"id": "status", "type": "string", "nullable": True,
     "source": {"column": "status"}},
    {"id": "amended_by", "type": "string", "nullable": True,
     "source": {"column": "amended_by"}},
    {"id": "amended_at", "type": "string", "nullable": True,
     "source": {"column": "amended_at"}},
]

#: The interfaces a drafted bundle may declare. Copied verbatim from the shape
#: the engine already relies on rather than re-derived: `Named` is what entity
#: resolution looks for, `DocumentScoped` is what stops two documents with the
#: same title being merged into one page, and a bundle that spells either of
#: them differently gets neither behaviour and no error.
_INTERFACES = {
    "Reviewable": {
        "id": "Reviewable",
        "display": {"name": "Reviewable",
                    "description": "Anything a person can confirm, amend or "
                                   "reject. Every extracted instance."},
        "requiredProperties": [
            {"id": "instance_id", "type": "string"},
            {"id": "document_id", "type": "string"},
            {"id": "source", "type": "string"},
            {"id": "confidence", "type": "number"},
            {"id": "status", "type": "string"},
        ],
    },
    "Named": {
        "id": "Named",
        "display": {"name": "Named entity",
                    "description": "An instance with a human name that might "
                                   "also appear in another document."},
        "requiredProperties": [
            {"id": "instance_id", "type": "string"},
            {"id": "document_id", "type": "string"},
            {"id": "name", "type": "string"},
            {"id": "naive_key", "type": "string"},
            {"id": "status", "type": "string"},
        ],
    },
    "DocumentScoped": {
        "id": "DocumentScoped",
        "display": {"name": "Document-scoped",
                    "description": "An instance whose identity is the document "
                                   "it was read from."},
        "requiredProperties": [
            {"id": "instance_id", "type": "string"},
            {"id": "document_id", "type": "string"},
            {"id": "status", "type": "string"},
        ],
    },
}


def _nameless_type_warnings(objects: list[dict], links: list[dict]) -> list[str]:
    """Types that will hold rows and never appear anywhere.

    The wiki is built from types implementing `Named`, which needs a `name`
    property, and the graph is a projection over wiki pages. So a type without
    one produces instances, produces no page, and every edge touching it is
    recorded as *structural* and never reaches the graph.

    That is a legitimate thing to want -- not everything is an entity -- so it
    is a warning and not a problem. But it is invisible until somebody looks at
    graph coverage and finds 8%, and by then the extraction has run. Measured
    on the council minutes: `Meeting` and `SteeringCouncil` were accepted
    without a name, and 625 of 794 extracted edges had nowhere to land.
    """
    warnings: list[str] = []
    named = {o["id"] for o in objects
             if any(p["id"] == "name" for p in o["properties"])}
    touched: dict[str, int] = defaultdict(int)
    for link in links:
        touched[link["from"]] += 1
        touched[link["to"]] += 1

    for obj in objects:
        if obj["id"] in named:
            continue
        if touched.get(obj["id"]):
            warnings.append(
                f"'{obj['id']}' has no `name` property, so it gets no wiki "
                f"page -- and every edge through the "
                f"{touched[obj['id']]} link type(s) that touch it will be "
                "recorded as structural and never reach the graph. Accept a "
                "property as `name`, or expect graph coverage to be low.")
        else:
            warnings.append(
                f"'{obj['id']}' has no `name` property, so it will hold rows "
                "and never appear in the wiki.")
    return warnings


def draft_bundle(store: Store, bundle_id: str, bundle_version: str = "0.1.0",
                 name: str | None = None, description: str | None = None,
                 primary_type: str | None = None,
                 document_types: list[str] | None = None,
                 document_scoped: list[str] | None = None,
                 sectors: list[str] | None = None,
                 jurisdictions: list[str] | None = None) -> dict:
    """Assemble a bundle from the candidates a person accepted.

    Nothing here decides anything. Every type, property and link in the result
    is one somebody accepted by hand, under the name they accepted it as; a
    candidate still sitting at `proposed` is not in the bundle, and neither is
    one that was rejected.

    What it does add is the parts a reviewer was never asked about because they
    are not domain knowledge: the provenance columns every instance table
    carries, the `Reviewable` interface that says a row can be confirmed, and
    `naive_key` alongside any accepted `name` — without which entity resolution
    has nothing to match on and the wiki is a list of unrelated pages.

    Returns the bundle and a `problems` list. It does not register it: a bundle
    goes into a store through `bundle.register`, the same way every other bundle
    does, and a drafting helper that quietly took that path would be the one
    place in this project where an ontology arrived without anybody choosing it.
    """
    require_string(bundle_id, "bundle_id")
    accepted = store.query(
        "SELECT * FROM ontology_candidates WHERE status IN ('accepted', "
        "'amended') ORDER BY kind, n_documents DESC, type_id")
    if not accepted:
        raise OrpheusError(
            "No candidate has been accepted, so there is nothing to draft. "
            "Run a survey and review what it proposes first.")

    # What each accepted type ended up being called, keyed by the id the survey
    # used, so a property proposed on `Record` follows it to `Proposal`.
    type_names: dict[str, str] = {}
    styles: dict[str, str | None] = {}
    for row in accepted:
        if row["kind"] == "object_type":
            type_names[row["type_id"]] = row["accepted_as"] or row["type_id"]
            styles[row["type_id"]] = row["name_style"]

    if not type_names:
        raise OrpheusError(
            "Candidates were accepted, but none of them was an object type, "
            "so there is nothing for the accepted properties to be properties "
            f"of: {', '.join(sorted(set(r['property_id'] or r['type_id'] for r in accepted)))}. "
            "A bundle drafted from this would have no types at all, which is "
            "the state the store is already in -- and registering it would "
            "look like the survey had been acted on.")

    objects: dict[str, dict] = {}
    for original, final in type_names.items():
        objects[original] = {
            "id": final,
            "display": {"name": final, "description": ""},
            "implements": ["Reviewable"],
            "primaryKey": "instance_id",
            "source": {"kind": "table", "table": f"instances_{final}"},
            "properties": [{"id": "instance_id", "type": "string",
                            "nullable": False,
                            "source": {"column": "instance_id"}}],
            "extensions": {"orpheus": {"managed": True}},
        }
        style = styles.get(original)
        if style in NAME_STYLES:
            objects[original]["extensions"]["orpheus"]["nameStyle"] = style

    problems: list[str] = []
    for row in accepted:
        if row["kind"] != "object_type":
            continue
        if row["display_name"] or row["description"]:
            obj = objects[row["type_id"]]
            obj["display"] = {"name": row["display_name"] or obj["id"],
                              "description": row["description"] or ""}

    for row in accepted:
        if row["kind"] != "property":
            continue
        if row["type_id"] not in objects:
            # Accepted on a type that was not. Reported rather than dropped
            # silently or attached to something: a property with no type is a
            # decision somebody made half of, and the half they missed is
            # visible only if this says so.
            problems.append(
                f"property '{row['property_id']}' was accepted on type "
                f"'{row['type_id']}', which was not — so it is not in the "
                "bundle. Accept the type, or accept the property on one that "
                "is.")
            continue
        property_id = row["accepted_as"] or row["property_id"]
        obj = objects[row["type_id"]]
        if any(p["id"] == property_id for p in obj["properties"]):
            continue
        obj["properties"].append({
            "id": property_id,
            "type": row["data_type"] or "string",
            "nullable": True,
            "display": {"name": row["display_name"] or property_id,
                        "description": row["description"] or ""},
            "source": {"column": property_id},
        })

    links = []
    for row in accepted:
        if row["kind"] != "link_type":
            continue
        if row["type_id"] not in objects or row["to_type_id"] not in objects:
            problems.append(
                f"link '{row['property_id']}' joins '{row['type_id']}' to "
                f"'{row['to_type_id']}', and one of those types was not "
                "accepted — so it is not in the bundle.")
            continue
        from_obj, to_obj = objects[row["type_id"]], objects[row["to_type_id"]]
        # A link needs a column to join on, and no survey proposes one: the
        # documents say a condition belongs to an application, not that the
        # condition row carries the application's key. So the foreign key is
        # added here, named after the type it points at, exactly as the
        # hand-written bundles name theirs.
        key = property_id_for(from_obj["id"]) + "_instance_id"
        if not any(p["id"] == key for p in to_obj["properties"]):
            to_obj["properties"].append({
                "id": key, "type": "string", "nullable": True,
                "display": {"name": f"{from_obj['id']} instance",
                            "description": f"The {from_obj['id']} this belongs "
                                           "to."},
                "source": {"column": key}})
        links.append({
            "id": row["accepted_as"] or row["property_id"],
            "from": from_obj["id"], "to": to_obj["id"],
            "display": {"name": row["display_name"] or row["property_id"],
                        "description": row["description"] or ""},
            "cardinality": "one-to-many",
            "join": {"fromKeys": ["instance_id"], "toKeys": [key]},
        })

    scoped = set(document_scoped or [])
    used_interfaces = {"Reviewable"}
    for original, obj in objects.items():
        names = [p["id"] for p in obj["properties"]]
        if "name" in names:
            if "naive_key" not in names:
                # Added, never asked about. `Named` is what the entity
                # resolution pass looks for and `naive_key` is what it matches
                # on; a bundle with a `name` and no key produces a wiki of
                # pages that can never be the same page as anything.
                obj["properties"].append({
                    "id": "naive_key", "type": "string", "nullable": True,
                    "display": {"name": "Naive key",
                                "description": "Normalised name, for naive "
                                               "matching only."},
                    "source": {"column": "naive_key"}})
            obj["implements"].append("Named")
            used_interfaces.add("Named")
        if obj["id"] in scoped or original in scoped:
            obj["implements"].append("DocumentScoped")
            used_interfaces.add("DocumentScoped")
        obj["properties"].extend(
            p for p in PROVENANCE_PROPERTIES
            if not any(q["id"] == p["id"] for q in obj["properties"]))

    ordered = list(objects.values())
    primary = primary_type
    if primary and primary not in {o["id"] for o in ordered}:
        raise OrpheusError(
            f"{primary!r} is not one of the accepted types "
            f"({', '.join(sorted(o['id'] for o in ordered))}).")
    if not primary and ordered:
        # The type with the most properties. A guess, and labelled as one in
        # the docs -- but a bundle with no primary type fails validation, and
        # refusing to draft over a field somebody can change in a text editor
        # would be the wrong place to stop.
        primary = max(ordered, key=lambda o: len(o["properties"]))["id"]

    from . import bundle as bundle_mod

    drafted = bundle_mod.normalise({
        "specVersion": "0.1.0",
        "bundleId": bundle_id,
        "bundleVersion": bundle_version,
        "metadata": {
            "name": name or bundle_id,
            "description": description or
            "Drafted from an ontology survey and reviewed by hand.",
            "createdAt": now(),
        },
        "objects": ordered,
        "links": links,
        "interfaces": [_INTERFACES[i] for i in
                       ("Reviewable", "Named", "DocumentScoped")
                       if i in used_interfaces],
        "extensions": {"orpheus": {
            "primaryObjectType": primary,
            "documentTypes": list(document_types or ["other"]),
            # Supplied by the drafter, never proposed. A survey reads what the
            # documents are *about*; which sectors and jurisdictions a
            # deployment cares to group by is a decision about the deployment.
            # Omitted when empty, and omitted means the classifier does not ask
            # -- which is the point: `sector` as an open question produced
            # thirteen spellings of one answer across forty-eight documents.
            **({"sectors": list(sectors)} if sectors else {}),
            **({"jurisdictions": list(jurisdictions)} if jurisdictions else {}),
        }},
    })

    try:
        bundle_mod.validate(drafted)
    except OrpheusError as invalid:
        problems.append(str(invalid))

    return {
        "bundle": drafted,
        "warnings": _nameless_type_warnings(ordered, links),
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
        "object_types": [o["id"] for o in ordered],
        "n_properties": sum(len(o["properties"]) for o in ordered),
        "links": [l["id"] for l in links],
        "primary_object_type": primary,
        "problems": problems,
        "valid": not problems,
    }
