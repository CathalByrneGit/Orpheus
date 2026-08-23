← [Back to index](index.md)

# OCDS alignment

The bundle's object types were invented. The
[Open Contracting Data Standard](https://standard.open-contracting.org/) already
defines this vocabulary, over 50 governments publish it, and it is what Irish
eTenders and EU TED data speak.

This page is the mapping and the proposed change. **The change is deliberately
additive** — no table is renamed and no test breaks. Renaming `Contract` to
`contracts` across nine object types, thirteen link types and 709 tests would be
a large change justified by nothing more than tidiness; carrying the mapping as
metadata gets the interoperability without the churn.

---

## Where Orpheus and OCDS actually differ

OCDS models a **contracting process** across five stages — planning, tender,
award, contract, implementation — with one `ocid` tying them together. Orpheus
models **one document at a time**, because Phase 1's job is reading a PDF, not
tracking a procurement through its lifecycle.

That is not a conflict, it is a difference in scope. A contract document is
evidence about one or two stages of a process. The mapping below says which
OCDS field each extracted fact corresponds to, so the day the corpus needs to
become an OCDS release, the translation is written down rather than
reconstructed.

---

## Object type mapping

### `Contract` → `contracts` (with `tender` context)

| Orpheus property | OCDS path | Note |
|---|---|---|
| `name` | `contracts/title` | |
| `reference` | `contracts/id` | OCDS also has `awardID` linking to the award |
| `description` | `contracts/description` | |
| `value_amount` | `contracts/value/amount` | |
| `value_currency` | `contracts/value/currency` | ISO 4217, as Orpheus already assumes |
| `start_date` | `contracts/period/startDate` | |
| `end_date` | `contracts/period/endDate` | |
| `signed_date` | `contracts/dateSigned` | |
| `procurement_procedure` | `tender/procurementMethod` | **Closed codelist — see below** |
| `governing_law` | — | No OCDS equivalent; stays an extension |
| `signature_block_present` | — | An extraction artefact, not contract data |

OCDS `contracts/status` is a closed codelist (`pending`, `active`, `cancelled`,
`terminated`). Orpheus does not extract it today and probably should — it is
usually stated, and it changes what a value means.

### `Company` → `parties`

| Orpheus property | OCDS path | Note |
|---|---|---|
| `name` | `parties/name` | |
| `registration_number` | `parties/identifier/id` | with `identifier/scheme`, e.g. `IE-CRO` |
| `address` | `parties/address` | OCDS structures this; Orpheus stores a string |
| `role` | `parties/roles[]` | **Codelist — see below.** OCDS allows several roles per party |
| `entity_kind` | — | No equivalent; stays an extension |
| `naive_key` | — | Orpheus-internal, never exported |

The `roles[]` plural is a real modelling difference: in OCDS one party can be
both `supplier` and `tenderer` in the same process. Orpheus stores a single
`role`, which is right for one document and will not survive cross-document work.

### `Person` → no direct equivalent

OCDS has `parties/contactPoint` (name, email, telephone) but does not model
individuals as first-class entities. Signatories, directors and officers are
exactly what conflict-of-interest work needs, so `Person` stays an Orpheus
extension. Worth knowing it is an extension rather than assuming it is standard.

### `Clause`, `Obligation`, `Flag`, `KeyDate`, `MonetaryAmount`

No OCDS equivalents. OCDS describes *what was procured from whom for how much*;
it does not model the text of the agreement. These are the parts of Orpheus that
are genuinely about reading documents, and they stay as they are.

---

## Codelists — the part worth adopting immediately

This is the highest-value piece, because it is not naming, it is correctness.

### `procurementMethod` (closed)

`open` · `selective` · `limited` · `direct`

Drawn from the WTO Government Procurement Agreement, with `direct` added for
awards without competition.

Orpheus currently stores free text, and the `direct_award` concept matches it
with:

```sql
LOWER(COALESCE(procurement_procedure,'')) IN
  ('direct award','direct_award','negotiated without prior publication')
```

That is a list of guesses about how a model might phrase it. Against a closed
codelist it becomes:

```sql
procurement_procedure = 'direct'
```

A concept that cannot silently miss a case because someone wrote "Direct Award"
with different capitalisation.

### `partyRole` (open)

`buyer` · `procuringEntity` · `supplier` · `tenderer` · `funder` · `enquirer` ·
`payer` · `payee` · `reviewBody` · `interestedParty`

Orpheus invented `contracting_authority`, `supplier`, `subcontractor`,
`guarantor`. Two of those map (`supplier`; `contracting_authority` → `buyer` or
`procuringEntity`), two do not exist in the core codelist — `subcontractor` comes
from the OCDS subcontracting extension.

Being an **open** codelist, extending it is legitimate. Using it means the values
are documented somewhere other than a comment in `make_bundle.R`.

### `contractStatus` (closed)

`pending` · `active` · `cancelled` · `terminated`

Not currently extracted. Proposed as a new `Contract` property.

---

## Proposed change

Additive only:

1. **An `x_ocds` mapping on each property** that has an OCDS equivalent, e.g.
   `"x_ocds": "contracts/period/startDate"`. Documentation that travels with the
   schema, and the basis for an OCDS export later.
2. **Codelist enums on the bundle**, so `procurement_procedure` and `role` carry
   their allowed values, and validation can check them.
3. **A `status` property on `Contract`** using `contractStatus`.
4. **`direct_award` rewritten** against the codelist instead of string guesses.
5. **The extraction prompt told about the codelists**, so the model is asked to
   classify into `open`/`selective`/`limited`/`direct` rather than to paraphrase.

Nothing is renamed. Nothing breaks. The bundle gains the vocabulary and the
mapping, and the day someone wants an OCDS release out of this corpus, the
translation is already written down.

---

## What was actually applied

All of the above, plus two things the work turned up.

**`contract_status`, not `status`.** OCDS calls its field `contracts/status`.
Taking that name literally collided with the `status` column every instance
table already has for the *review* state — two different meanings on one column,
in the one place ambiguity is least affordable. Bundle validation now rejects
duplicate property ids, because this class of mistake produced a table with two
columns of one name and nothing noticed until the JSON was inspected.

**Codelist values are reported, never rejected.** `orph_codelist_violations()`
lists values recorded outside the codelist that governs them:

```r
orph_codelist_violations(con)
#>   type_id  property               value                  n  x_ocds
#> 1 Contract procurement_procedure  Direct Award           1  tender/procurementMethod
#> 2 Company  role                   contracting_authority  1  parties/roles
```

The value stays in the table. That follows the same rule as an unrecognised
property becoming a schema amendment: **the mismatch is the signal.** A model
writing "Direct Award" instead of `direct` means the prompt needs work; a
genuine role the codelist lacks means the codelist needs extending. Rejecting
the write would destroy the evidence for either conclusion.

This matters because concepts are now written against the codelist —
`direct_award` is `procurement_procedure = 'direct'` — so a non-conforming value
is **silently missed** by the concept. The report is what stops that silence
being invisible.

The extraction prompt now names each codelist inline:

```
      procurement_procedure (string): How the contract was awarded, OCDS procurementMethod codelist (closed)
        MUST be exactly one of: open, selective, limited, direct
```

---

## What is deliberately not proposed

**Restructuring to OCDS's release model.** OCDS is built around an `ocid` and a
process spanning five stages. Orpheus extracts from one document at a time and
has no notion of a process. Adopting the release model would mean entity
resolution — knowing that this contract and that tender notice are the same
procurement — which is Phase 4 and explicitly out of scope. Doing it now would be
building the join before there is anything to join.

**Renaming object types to OCDS names.** `contracts` and `parties` are OCDS's
names for arrays in a release document, not for entity types. Renaming for
surface similarity would obscure that the underlying models differ, at the cost
of touching everything.

---

[← Back to index](index.md) | [Prior art →](prior-art.md)
