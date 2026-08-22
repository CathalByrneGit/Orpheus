# Build the core contract ontology bundle.
#
# Run:  Rscript data-raw/make_bundle.R
#
# The output is inst/bundles/contract-core-0.1.0.json. It is a normal data
# artifact: `dis_to_bundle()` output from a real ontologyDiscoverR discovery
# run over a sample of contracts can replace it wholesale, and this script
# exists so the hand-seeded starting bundle is reproducible rather than
# hand-edited JSON.
#
# ---------------------------------------------------------------------------
# Why the field names are duplicated
# ---------------------------------------------------------------------------
# Three packages read this bundle and each reads different keys for the same
# idea. Rather than convert at every call site, the bundle carries every
# spelling the consumers look for:
#
#   ontologyDiscoverR  object_types / link_types, link$from_type_id/to_type_id
#   conceptR           object_type$table_name, object_type$primary_key
#   objectSetsR        object_type$source$table, link$from/$to, link$join$fromKeys
#
# Anything reading this bundle therefore works unmodified. The alternative --
# picking one convention and shimming the other two -- puts a translation step
# on every path instead of a few redundant keys in one file.

`%||%` <- function(x, y) if (is.null(x)) y else x

# Provenance columns are declared as ordinary properties, not hidden metadata.
# objectSetsR projects an object set down to declared properties only, so a
# query that cannot see `status` cannot exclude rejected rows -- which would
# make every corpus-wide answer quietly wrong.
provenance_props <- function() list(
  prop("document_id", "string",  "Document this instance was extracted from"),
  prop("source",      "string",  "ai_local | ai_cloud | human"),
  prop("confidence",  "double",  "Confidence rubric level"),
  prop("status",      "string",  "unconfirmed | confirmed | amended | rejected"),
  prop("amended_by",  "string",  "Actor who last amended this row", nullable = TRUE),
  prop("amended_at",  "string",  "When this row was last amended",  nullable = TRUE)
)

prop <- function(id, type, description, nullable = TRUE, column = NULL) {
  list(
    id          = id,
    type        = type,
    nullable    = nullable,
    description = description,
    # objectSetsR reads property$source$column when mapping a property onto a
    # physical column. Identity mappings are still stated so a later rename of
    # a column does not require a code change, only a bundle edit.
    source      = list(column = column %||% id)
  )
}

# Interfaces are a contract several object types share, so a question can be
# asked once across all of them instead of once per type. Taken from
# ontologySpecR's interface_type / objectSetsR's object_set_by_interface.
#
# Same dual-spelling problem as everywhere else in the stack: ontologySpecR
# emits `requiredProperties`, objectSetsR reads `properties`. Both are written.
interface_type <- function(id, display_name, description, properties) {
  list(
    id                 = id,
    display_name       = display_name,
    description        = description,
    properties         = properties,   # objectSetsR
    requiredProperties = properties    # ontologySpecR
  )
}

# Which interfaces each object type satisfies. Kept as one table rather than an
# argument at nine call sites: the point of an interface is that the set of
# types answering a question is visible in one place, and spreading it across
# the constructors would defeat that.
IMPLEMENTS <- list(
  Contract       = c("Reviewable"),
  Company        = c("Reviewable", "Named"),
  Person         = c("Reviewable", "Named"),
  Clause         = c("Reviewable", "PageAnchored"),
  Obligation     = c("Reviewable"),
  Flag           = c("Reviewable"),
  KeyDate        = c("Reviewable", "PageAnchored"),
  MonetaryAmount = c("Reviewable", "PageAnchored")
  # Relationship implements nothing: it is an edge, not an extracted instance,
  # and its primary key is edge_id rather than instance_id.
)

object_type <- function(id, display_name, description, properties,
                        table_name = NULL, managed = TRUE) {
  table_name <- table_name %||% paste0("instances_", id)
  list(
    id           = id,
    display_name = display_name,
    description  = description,
    implements   = as.list(IMPLEMENTS[[id]] %||% character()),
    # conceptR reads these two directly.
    table_name   = table_name,
    primary_key  = "instance_id",
    primaryKey   = "instance_id",
    # objectSetsR reads source$table.
    source       = list(kind = "table", table = table_name),
    properties   = c(list(prop("instance_id", "string", "Instance identifier", nullable = FALSE)),
                     properties, provenance_props()),
    # Orpheus-specific: whether the store owns this table's DDL. Relationship
    # is backed by the hand-written `edges` table, so schema generation must
    # leave it alone.
    x_orpheus    = list(managed = managed)
  )
}

link_type <- function(id, from, to, from_keys, to_keys, display_name, description,
                      cardinality = "many-to-many", directed = TRUE) {
  list(
    id           = id,
    display_name = display_name,
    description  = description,
    from         = from,          # objectSetsR
    to           = to,
    from_type_id = from,          # ontologyDiscoverR
    to_type_id   = to,
    cardinality  = cardinality,
    directed     = directed,
    join         = list(fromKeys = as.list(from_keys), toKeys = as.list(to_keys))
  )
}

# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------
# Each of these exists because a real question spans several object types and
# would otherwise be asked once per type, in code, with the list of types
# hard-coded at the call site -- which is how a new object type silently stops
# being included in an answer.

interfaces <- list(
  interface_type("Reviewable", "Reviewable",
    "Anything a person can confirm, amend or reject. Every extracted instance.",
    list(
      prop("instance_id", "string", "Instance identifier", nullable = FALSE),
      prop("document_id", "string", "Document it was extracted from"),
      prop("source",      "string", "ai_local | ai_cloud | human"),
      prop("confidence",  "double", "Confidence rubric level"),
      prop("status",      "string", "unconfirmed | confirmed | amended | rejected")
    )),

  interface_type("Named", "Named entity",
    "An instance with a human name that might also appear in another document.",
    list(
      prop("instance_id", "string", "Instance identifier", nullable = FALSE),
      prop("document_id", "string", "Document it was extracted from"),
      prop("name",        "string", "Name as written in the document", nullable = FALSE),
      prop("naive_key",   "string", "Normalised name for best-effort matching"),
      prop("status",      "string", "Review status")
    )),

  interface_type("PageAnchored", "Page-anchored",
    "An instance that can be pointed at on a specific page of the document.",
    list(
      prop("instance_id", "string", "Instance identifier", nullable = FALSE),
      prop("document_id", "string", "Document it was extracted from"),
      prop("page_no",     "integer","Page it appears on"),
      prop("status",      "string", "Review status")
    ))
)

# ---------------------------------------------------------------------------
# Object types
# ---------------------------------------------------------------------------

object_types <- list(
  object_type("Contract", "Contract",
    "An agreement between a public body and one or more other parties.",
    list(
      prop("name",                   "string", "Title of the agreement", nullable = FALSE),
      prop("reference",              "string", "Contract or tender reference number"),
      prop("description",            "string", "Short description of the subject matter"),
      prop("value_amount",           "double", "Headline contract value"),
      prop("value_currency",         "string", "ISO currency code of the headline value"),
      prop("start_date",             "string", "Commencement date (ISO-8601)"),
      prop("end_date",               "string", "Expiry date (ISO-8601); null if open-ended"),
      prop("signed_date",            "string", "Date of execution (ISO-8601)"),
      prop("procurement_procedure",  "string", "e.g. open, restricted, negotiated, direct award"),
      prop("governing_law",          "string", "Jurisdiction whose law governs the agreement"),
      prop("signature_block_present","string", "Whether a signature block was found: yes | no | unclear")
    )),

  object_type("Company", "Company",
    "A legal entity party to, or named in, an agreement. Includes public bodies.",
    list(
      prop("name",             "string", "Name as written in the document", nullable = FALSE),
      prop("naive_key",        "string", "Normalised name for best-effort cross-document matching"),
      prop("registration_number", "string", "Company registration number, if stated"),
      prop("address",          "string", "Registered or stated address"),
      prop("role",             "string", "e.g. contracting_authority, supplier, subcontractor, guarantor"),
      prop("entity_kind",      "string", "e.g. private_company, public_body, charity, partnership")
    )),

  object_type("Person", "Person",
    "A named individual appearing in an agreement.",
    list(
      prop("name",        "string", "Name as written in the document", nullable = FALSE),
      prop("naive_key",   "string", "Normalised name for best-effort cross-document matching"),
      prop("job_title",   "string", "Stated role or title"),
      prop("acting_for",  "string", "Organisation the person is stated to act for")
    )),

  object_type("Clause", "Clause",
    "A numbered or titled provision within an agreement.",
    list(
      prop("contract_instance_id", "string", "Contract this clause belongs to"),
      prop("clause_number",  "string", "Clause number as printed"),
      prop("heading",        "string", "Clause heading"),
      prop("clause_type",    "string", "e.g. indemnity, liability, termination, confidentiality, payment, renewal"),
      prop("text",           "string", "Clause text as extracted"),
      prop("page_no",        "integer","Page the clause starts on")
    )),

  object_type("Obligation", "Obligation",
    "A duty a clause places on a named party.",
    list(
      prop("clause_instance_id", "string", "Clause imposing this obligation"),
      prop("obligated_party",    "string", "Party bound, as named in the text"),
      prop("summary",            "string", "What must be done"),
      prop("due_date",           "string", "Deadline, if stated (ISO-8601)"),
      prop("recurrence",         "string", "e.g. one_off, monthly, quarterly, annual")
    )),

  object_type("Flag", "Flag",
    "An issue raised against a document or one of its clauses.",
    list(
      prop("target_instance_id", "string", "Instance the flag is raised against"),
      prop("flag_type",   "string", "e.g. unusual_indemnity, missing_signature, ambiguous_term, uncapped_liability"),
      prop("severity",    "string", "low | medium | high"),
      prop("rationale",   "string", "Why this was flagged"),
      prop("raised_by_pass", "string", "Which pass raised it: local | cloud | concept | human")
    )),

  # KeyDate and MonetaryAmount exist so the deterministic regex pass has
  # somewhere to write a finding with its own page-level provenance, rather
  # than silently overwriting a Contract property the LLM also populated.
  object_type("KeyDate", "Key date",
    "A date found in the document, with the phrase that introduced it.",
    list(
      prop("contract_instance_id", "string", "Contract this date was found in"),
      prop("value",     "string", "Normalised date (ISO-8601)"),
      prop("raw_text",  "string", "Date exactly as printed"),
      prop("date_role", "string", "e.g. start, end, signature, milestone, unknown"),
      prop("page_no",   "integer","Page the date appears on")
    )),

  object_type("MonetaryAmount", "Monetary amount",
    "A monetary value found in the document.",
    list(
      prop("contract_instance_id", "string", "Contract this amount was found in"),
      prop("amount",    "double", "Numeric value"),
      prop("currency",  "string", "ISO currency code"),
      prop("raw_text",  "string", "Amount exactly as printed"),
      prop("role",      "string", "e.g. contract_value, cap, penalty, rate, unknown"),
      prop("page_no",   "integer","Page the amount appears on")
    )),

  # Backed by the `edges` table. objectSetsR joins object tables directly on
  # key columns, so a many-to-many link cannot be traversed in one hop; making
  # the edge itself an object type lets os_traverse() reach it in two, which
  # is how the many-to-many links below are declared.
  object_type("Relationship", "Relationship",
    "A link between two instances, as extracted from a document.",
    list(
      prop("from_instance_id", "string", "Source instance", nullable = FALSE),
      prop("to_instance_id",   "string", "Target instance", nullable = FALSE),
      prop("link_type_id",     "string", "Which link type this edge instantiates", nullable = FALSE),
      prop("evidence",         "string", "Text supporting the link")
    ),
    table_name = "edges", managed = FALSE)
)

# Relationship's physical PK column is edge_id, not instance_id.
for (i in seq_along(object_types)) {
  if (object_types[[i]]$id == "Relationship") {
    object_types[[i]]$primary_key <- "edge_id"
    object_types[[i]]$primaryKey  <- "edge_id"
    object_types[[i]]$properties[[1]] <- prop("edge_id", "string", "Edge identifier",
                                              nullable = FALSE)
  }
}

# ---------------------------------------------------------------------------
# Link types
# ---------------------------------------------------------------------------

link_types <- list(
  # Containment links are denormalised onto the child table, so they traverse
  # in a single join.
  link_type("contains_clause", "Contract", "Clause",
            "instance_id", "contract_instance_id",
            "contains clause", "Clause belongs to this contract", "one-to-many"),
  link_type("imposes", "Clause", "Obligation",
            "instance_id", "clause_instance_id",
            "imposes", "Obligation arises from this clause", "one-to-many"),
  link_type("dated_by", "Contract", "KeyDate",
            "instance_id", "contract_instance_id",
            "has key date", "Date found in this contract", "one-to-many"),
  link_type("valued_at", "Contract", "MonetaryAmount",
            "instance_id", "contract_instance_id",
            "has amount", "Monetary amount found in this contract", "one-to-many"),

  # Many-to-many links, traversed through Relationship.
  link_type("edge_from", "Relationship", "Contract",
            "from_instance_id", "instance_id",
            "edge source", "Instance an edge starts at", "many-to-one"),
  link_type("edge_to", "Relationship", "Company",
            "to_instance_id", "instance_id",
            "edge target", "Instance an edge points to", "many-to-one"),

  # Semantic many-to-many link types. These are the vocabulary the extraction
  # pass writes into edges.link_type_id; traversal of them goes via
  # Relationship above.
  link_type("party_to", "Company", "Contract",
            "instance_id", "instance_id",
            "party to", "Company is a party to this contract"),
  link_type("signed_by", "Person", "Contract",
            "instance_id", "instance_id",
            "signed", "Person signed this contract"),
  link_type("subcontracts_to", "Company", "Company",
            "instance_id", "instance_id",
            "subcontracts to", "Company subcontracts work to another company"),
  link_type("references", "Contract", "Contract",
            "instance_id", "instance_id",
            "references", "Contract refers to another agreement"),
  link_type("mentions", "Clause", "Company",
            "instance_id", "instance_id",
            "mentions", "Clause names a company"),
  link_type("employed_by", "Person", "Company",
            "instance_id", "instance_id",
            "employed by", "Person acts for this company"),
  link_type("raised_against", "Flag", "Clause",
            "instance_id", "instance_id",
            "raised against", "Flag concerns this clause")
)

# ---------------------------------------------------------------------------
# Seed concepts
# ---------------------------------------------------------------------------
# conceptR concepts are versioned SQL boolean expressions evaluated per row,
# so they are the deterministic half of document analysis: reproducible,
# reviewable, and diffable between versions. The interpretive half (summary,
# recommendations) is an LLM pass and is stored separately -- see R/concepts.R.
#
# Note these expressions are interpolated into SQL by conceptR, so authoring a
# concept is a privileged operation. The API restricts it to admin actors.

# Templates exist for the concepts whose expression is right but whose numbers
# are a local policy question. A hardcoded threshold is a guess wearing the
# authority of code: templating it means a deployment sets its own review
# threshold without editing the bundle, and every change is a new concept
# version rather than a silent edit.
concept_templates <- list(
  list(
    template_id    = "value_threshold",
    object_type_id = "Contract",
    description    = "Contract value at or above a configurable review threshold.",
    base_sql_expr  = "value_amount IS NOT NULL AND CAST(value_amount AS REAL) >= {{threshold}}",
    parameters     = list(
      threshold = list(type = "double", default = 1000000,
                       description = "Value at or above which a contract is treated as high value, in the contract's own currency.")
    )
  )
)

# A deterministic counterpart to the narrative risk level. The model's
# risk_level is interpretation and is not reproducible; this is arithmetic over
# concepts that have already been evaluated, so it can be explained, diffed
# between versions, and disagreed with. Where the two diverge is worth a look --
# which is the point of having both.
scores <- list(
  list(
    score_id       = "contract_risk",
    object_type_id = "Contract",
    aggregation    = "weighted_sum",
    description    = "Weighted count of the risk concepts a contract triggers.",
    thresholds     = list(low = 0, medium = 2, high = 4),
    components     = list(
      list(concept_id = "missing_signature", scope = "compliance",  weight = 2),
      list(concept_id = "direct_award",      scope = "procurement", weight = 2),
      list(concept_id = "high_value",        scope = "commercial",  weight = 1),
      list(concept_id = "open_ended_term",   scope = "commercial",  weight = 1)
    )
  )
)

concept_defs <- list(
  list(id = "high_value", object_type_id = "Contract", scope = "commercial",
       display_name = "High value",
       description = "Headline value at or above the review threshold.",
       template_id = "value_threshold",
       parameter_values = list(threshold = 1000000),
       rationale = "Threshold is a deployment setting, not a fact. Override it with orph_set_concept_parameter()."),
  list(id = "missing_signature", object_type_id = "Contract", scope = "compliance",
       display_name = "Missing signature block",
       description = "No signature block was found in the document.",
       sql_expr = "signature_block_present = 'no'",
       rationale = "A contract with no signature block needs a human to confirm execution."),
  list(id = "open_ended_term", object_type_id = "Contract", scope = "commercial",
       display_name = "Open-ended term",
       description = "No expiry date was extracted.",
       sql_expr = "end_date IS NULL OR end_date = ''",
       rationale = "Distinguishing a genuinely open-ended term from a failed extraction is exactly what human review is for."),
  list(id = "direct_award", object_type_id = "Contract", scope = "procurement",
       display_name = "Direct award",
       description = "Awarded without a competitive procedure.",
       sql_expr = "LOWER(COALESCE(procurement_procedure,'')) IN ('direct award','direct_award','negotiated without prior publication')",
       rationale = "Direct awards attract the most procurement scrutiny."),
  list(id = "uncapped_liability", object_type_id = "Clause", scope = "risk",
       display_name = "Uncapped liability",
       description = "A liability clause with no cap language.",
       sql_expr = "clause_type = 'liability' AND LOWER(COALESCE(text,'')) NOT LIKE '%cap%' AND LOWER(COALESCE(text,'')) NOT LIKE '%limited to%'",
       rationale = "Coarse by design: it over-selects, and review narrows it."),
  list(id = "auto_renewal", object_type_id = "Clause", scope = "commercial",
       display_name = "Automatic renewal",
       description = "A clause that renews the agreement without a positive decision.",
       sql_expr = "LOWER(COALESCE(text,'')) LIKE '%automatically renew%' OR LOWER(COALESCE(text,'')) LIKE '%unless terminated%'",
       rationale = "Auto-renewal is a common source of unintended spend.")
)

bundle <- list(
  bundle_id    = "contract-core",
  bundle_name  = "Core contract ontology",
  version      = "0.1.0",
  spec_version = "0.1.0",
  created_at   = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
  description  = paste(
    "Hand-seeded starting bundle for Phase 1. Replaceable by dis_to_bundle()",
    "output from an ontologyDiscoverR discovery run over a real contract sample."
  ),
  object_types = object_types,
  interfaces   = interfaces,
  link_types   = link_types,
  concept_templates = concept_templates,
  scores       = scores,
  action_types = list(),
  concept_defs = concept_defs
)

# Aliases so a consumer reading the ontologySpecR spelling finds the same data.
bundle$objects        <- bundle$object_types
bundle$links          <- bundle$link_types
bundle$interfaceTypes <- bundle$interfaces
bundle$concepts <- bundle$concept_defs

dir.create("inst/bundles", recursive = TRUE, showWarnings = FALSE)
out <- "inst/bundles/contract-core-0.1.0.json"
writeLines(jsonlite::toJSON(bundle, auto_unbox = TRUE, pretty = TRUE, null = "null"), out)
cat("wrote", out, "\n")
cat("object types:", length(object_types), " interfaces:", length(interfaces),
    " link types:", length(link_types), " concepts:", length(concept_defs),
    " templates:", length(concept_templates), " scores:", length(scores), "\n")
