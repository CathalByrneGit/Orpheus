# ---------------------------------------------------------------------------
# Pipeline step 9: database-wide analysis.
#
# A deliberate, user-triggered escalation -- heavier than a single-document
# pass, and it does not wait for entity resolution. Matching is on normalised
# raw name text, which is a stepping stone to Phase 4's real resolution and not
# a substitute for it. Every result carries resolution_quality =
# "naive_unresolved" so nothing downstream can mistake it for resolved data.
#
# What that means in practice, and why it is stated on every result: the naive
# key matches "Ernst & Young LLP" to "Ernst & Young", and fails to match either
# to "Ernst and Young". It over-merges distinct entities that share a name and
# under-merges one entity written two ways. That is acceptable for a
# best-effort "has this name appeared elsewhere?" and unacceptable as a basis
# for a conflict-of-interest finding.
# ---------------------------------------------------------------------------

#' Quality label attached to every naive-matched result
#' @export
ORPH_NAIVE_RESOLUTION <- "naive_unresolved"

#' @keywords internal
have_objectsets <- function() requireNamespace("objectSetsR", quietly = TRUE)

# ---------------------------------------------------------------------------
# Interface queries
# ---------------------------------------------------------------------------

#' Query every object type that implements an interface, as one set
#'
#' The ontology's job is to let a question be asked once. "Which named entities
#' appear in this document" spans Company and Person today and may span more
#' tomorrow; asking it per type means the list of types lives at the call site,
#' and a new type silently stops being included in the answer. Asking it by
#' interface means adding `"Named"` to a new object type is enough.
#'
#' Mirrors `objectSetsR::object_set_by_interface()`: the result is projected to
#' the interface's properties only, so every row has the same shape whichever
#' type it came from. A `type_id` column says which that was.
#'
#' @param con A connection.
#' @param interface_id Interface identifier, e.g. `"Named"`.
#' @param bundle A bundle. Defaults to the active one.
#' @param document_id Restrict to one document, or `NULL` for the corpus.
#' @param include_rejected Include rows a reviewer rejected.
#' @param where Optional extra SQL predicate over the interface's properties.
#' @param params Parameters for `where`.
#' @return A data frame with the interface's properties plus `type_id`.
#' @export
orph_object_set_by_interface <- function(con, interface_id, bundle = NULL,
                                         document_id = NULL, include_rejected = FALSE,
                                         where = NULL, params = list()) {
  bundle <- bundle %||% orph_active_bundle(con)
  if (is.null(bundle)) cli::cli_abort("No active ontology bundle.")

  iface <- orph_interface(bundle, interface_id)
  if (is.null(iface)) {
    known <- vapply(bundle$interfaces %||% list(), function(i) i$id %||% "", character(1))
    cli::cli_abort(c("No interface {.val {interface_id}} in the bundle.",
                     i = "Known interfaces: {.val {known}}"))
  }

  type_ids <- orph_implementing_types(bundle, interface_id)
  if (length(type_ids) == 0) {
    cli::cli_abort("No object type implements {.val {interface_id}}.")
  }

  cols <- orph_interface_property_ids(iface)
  selects <- character(); all_params <- list()

  for (tid in type_ids) {
    ot <- orph_object_type(bundle, tid)
    if (is.null(ot) || !DBI::dbExistsTable(con, ot$table_name)) next

    # Validated at registration, but a table can lag its bundle between an
    # amendment being accepted and the schema being applied.
    present <- DBI::dbListFields(con, ot$table_name)
    if (!all(cols %in% present)) next

    clauses <- character()
    if (!include_rejected) clauses <- c(clauses, "status != 'rejected'")
    if (!is.null(document_id)) { clauses <- c(clauses, "document_id = ?"); all_params <- c(all_params, document_id) }
    if (!is.null(where))       { clauses <- c(clauses, paste0("(", where, ")")); all_params <- c(all_params, params) }

    selects <- c(selects, sprintf(
      "SELECT %s, %s AS type_id FROM %s%s",
      paste(DBI::dbQuoteIdentifier(con, cols), collapse = ", "),
      DBI::dbQuoteString(con, tid),
      DBI::dbQuoteIdentifier(con, ot$table_name),
      if (length(clauses)) paste0(" WHERE ", paste(clauses, collapse = " AND ")) else ""))
  }

  if (length(selects) == 0) {
    empty <- stats::setNames(
      lapply(seq_along(c(cols, "type_id")), function(i) character()), c(cols, "type_id"))
    return(as.data.frame(empty, stringsAsFactors = FALSE))
  }

  db_query(con, paste(selects, collapse = "\nUNION ALL\n"), all_params)
}

#' Collect an object type's live rows
#'
#' Uses objectSetsR when it is installed, which is what makes this an object-set
#' query over the populated ontology rather than a bespoke one. The direct SQL
#' path is a fallback so a deployment without the package still gets the
#' escalation, and both paths are held to the same projection: declared
#' properties only, rejected rows excluded.
#'
#' @param con A connection.
#' @param bundle A bundle.
#' @param type_id Object type identifier.
#' @param naive_keys Restrict to these naive keys, or `NULL` for all rows.
#' @return A data frame.
#' @keywords internal
collect_object_set <- function(con, bundle, type_id, naive_keys = NULL) {
  ot <- orph_object_type(bundle, type_id)
  if (is.null(ot) || !DBI::dbExistsTable(con, ot$table_name)) return(data.frame())

  if (have_objectsets()) {
    result <- tryCatch({
      b <- bundle; class(b) <- c("ontology_bundle", "list")
      ctx <- objectSetsR::ontology_context(b, con, check_interfaces = FALSE)
      os  <- objectSetsR::object_set(ctx, type_id)
      os  <- objectSetsR::os_filter(os, .data$status != "rejected")
      if (!is.null(naive_keys)) {
        keys <- unique(naive_keys)
        os <- objectSetsR::os_filter(os, .data$naive_key %in% !!keys)
      }
      as.data.frame(objectSetsR::os_collect(os))
    }, error = function(e) {
      cli::cli_warn(c("objectSetsR query failed; falling back to direct SQL.",
                      x = conditionMessage(e)))
      NULL
    })
    if (!is.null(result)) return(result)
  }

  cols <- intersect(orph_property_ids(ot), DBI::dbListFields(con, ot$table_name))
  sql <- sprintf("SELECT %s FROM %s WHERE status != 'rejected'",
                 paste(DBI::dbQuoteIdentifier(con, cols), collapse = ", "),
                 DBI::dbQuoteIdentifier(con, ot$table_name))
  params <- list()
  if (!is.null(naive_keys)) {
    keys <- unique(naive_keys)
    if (length(keys) == 0) return(data.frame())
    sql <- paste0(sql, sprintf(" AND naive_key IN (%s)",
                               paste(rep("?", length(keys)), collapse = ", ")))
    params <- as.list(keys)
  }
  db_query(con, sql, params)
}

#' Run the database-wide analysis for a document
#'
#' Answers the questions the architecture names for this step: do the companies
#' and people in this document appear in others, and how does this contract's
#' value sit against other contracts involving the same counterparties.
#'
#' @param con A writable connection.
#' @param document_id The document to compare against the rest of the corpus.
#' @param actor_id Actor triggering the escalation.
#' @param narrate Also produce a narrative reading of the findings. Requires a
#'   cloud opt-in when `tier = "cloud"`.
#' @param tier Tier for the optional narrative.
#' @param opt_in Explicit cloud opt-in for the narrative.
#' @return A list with the findings and the evaluation id they were stored under.
#' @export
orph_corpus_analysis <- function(con, document_id, actor_id = NULL, narrate = FALSE,
                                 tier = c("cloud", "local"), opt_in = FALSE) {
  assert_writable(con)
  tier   <- match.arg(tier)
  bundle <- orph_active_bundle(con)
  doc    <- orph_get_document(con, document_id)
  if (is.null(doc)) cli::cli_abort("No document {.val {document_id}}.")

  n_docs <- db_get_one(con, "SELECT COUNT(*) AS n FROM documents")$n
  if (n_docs < 2) {
    cli::cli_abort(c(
      "Database-wide analysis needs more than one document; the store has {n_docs}.",
      i = "Ingest and extract at least one more document first."))
  }

  findings <- list(
    counterparties = match_counterparties(con, bundle, document_id, "Company"),
    people         = match_counterparties(con, bundle, document_id, "Person")
  )
  findings$value_comparison <- compare_contract_values(con, bundle, document_id,
                                                       findings$counterparties)

  context_ids <- unique(c(
    unlist(lapply(findings$counterparties, function(f) f$other_instance_ids)),
    unlist(lapply(findings$people,         function(f) f$other_instance_ids))))
  local_ids <- unique(c(
    vapply(findings$counterparties, function(f) f$instance_id, character(1)),
    vapply(findings$people,         function(f) f$instance_id, character(1))))

  result <- list(
    document_id     = document_id,
    matched_companies = length(findings$counterparties),
    matched_people    = length(findings$people),
    counterparties  = findings$counterparties,
    people          = findings$people,
    value_comparison = findings$value_comparison,
    caveat = paste(
      "Matching is on normalised raw name text, not resolved entities.",
      "Two different organisations sharing a name will be merged, and one",
      "organisation written two ways will not be. Treat these as leads to check,",
      "not as findings.")
  )

  narrative <- NULL
  if (isTRUE(narrate)) {
    if (tier == "cloud") orph_assert_cloud_allowed(con, opt_in = opt_in, actor_id = actor_id)
    reply <- orph_llm_json(con, tier, corpus_system_prompt(),
                           as.character(to_json(result)),
                           purpose = "corpus_analysis", document_id = document_id,
                           actor_id = actor_id, excerpt_only = TRUE, opt_in = opt_in)
    narrative <- list(summary = as_scalar_or_na(reply$summary),
                      observations = reply$observations %||% list(),
                      suggested_checks = reply$suggested_checks %||% list())
    result$narrative <- narrative
  }

  eval_id <- NULL
  with_tx(con, {
    eval_id <- write_concept_evaluation(con, bundle,
      concept_id = "corpus_comparison", concept_version = NA_integer_,
      concept_scope = NA_character_, kind = "corpus", scope_level = "database",
      document_id = document_id, result = result,
      dependencies = c(local_ids, context_ids),
      source = if (isTRUE(narrate) && tier == "cloud") "ai_cloud" else "ai_local",
      confidence = unname(ORPH_CONFIDENCE[["inferred"]]),
      actor_id = actor_id,
      corpus_context = list(instance_ids = context_ids,
                            document_ids = unique(unlist(lapply(
                              c(findings$counterparties, findings$people),
                              function(f) f$other_document_ids)))),
      resolution_quality = ORPH_NAIVE_RESOLUTION)
  })

  c(result, list(evaluation_id = eval_id, resolution_quality = ORPH_NAIVE_RESOLUTION,
                 engine = if (have_objectsets()) "objectSetsR" else "sql_fallback"))
}

#' @keywords internal
match_counterparties <- function(con, bundle, document_id, type_id) {
  # The instances in *this* document still come from one type -- the caller asks
  # about companies or about people. What changed is the lookup: it now searches
  # every type implementing `Named`, not just the same type. A name is a name,
  # and whether the match lands on a Company or a Person is a finding rather
  # than something to filter out in advance.
  ot <- orph_object_type(bundle, type_id)
  if (is.null(ot) || !DBI::dbExistsTable(con, ot$table_name)) return(list())

  mine <- db_query(con, sprintf(
    "SELECT instance_id, name, naive_key FROM %s
     WHERE document_id = ? AND status != 'rejected' AND naive_key IS NOT NULL AND naive_key != ''",
    DBI::dbQuoteIdentifier(con, ot$table_name)), list(document_id))
  if (nrow(mine) == 0) return(list())

  named <- orph_object_set_by_interface(con, "Named", bundle = bundle)
  if (nrow(named) == 0) return(list())
  named <- named[!is.na(named$naive_key) & nzchar(named$naive_key), , drop = FALSE]

  out <- list()
  for (i in seq_len(nrow(mine))) {
    key <- mine$naive_key[[i]]
    others <- named[named$naive_key %in% key & named$document_id != document_id, , drop = FALSE]
    if (nrow(others) == 0) next

    same_type  <- others[others$type_id == type_id, , drop = FALSE]
    other_type <- others[others$type_id != type_id, , drop = FALSE]
    variants   <- unique(others$name)

    out[[length(out) + 1L]] <- list(
      instance_id          = mine$instance_id[[i]],
      name                 = mine$name[[i]],
      naive_key            = key,
      appears_in_documents = length(unique(same_type$document_id)),
      other_document_ids   = unique(same_type$document_id),
      other_instance_ids   = unique(same_type$instance_id),
      name_variants        = variants,
      # Surfaced rather than hidden: differing spellings under one key are the
      # clearest signal that this needs real resolution.
      spelling_varies      = length(setdiff(variants, mine$name[[i]])) > 0,
      # A name that is a company here and a person elsewhere is exactly the
      # shape of thing a reviewer wants to look at, so it is reported rather
      # than quietly dropped -- and reported separately, because it is a
      # weaker signal than a same-type match.
      cross_type_matches   = if (nrow(other_type) == 0) list() else lapply(
        seq_len(nrow(other_type)), function(k) list(
          type_id     = other_type$type_id[[k]],
          name        = other_type$name[[k]],
          instance_id = other_type$instance_id[[k]],
          document_id = other_type$document_id[[k]]))
    )
  }
  out
}

#' @keywords internal
compare_contract_values <- function(con, bundle, document_id, counterparties) {
  this <- db_get_one(con,
    "SELECT instance_id, name, value_amount, value_currency FROM instances_Contract
     WHERE document_id = ? AND status != 'rejected' ORDER BY confidence DESC LIMIT 1",
    list(document_id))
  if (is.null(this) || is.na(this$value_amount)) {
    return(list(available = FALSE,
                reason = "No contract value has been extracted for this document."))
  }

  other_docs <- unique(unlist(lapply(counterparties, function(f) f$other_document_ids)))
  if (length(other_docs) == 0) {
    return(list(available = FALSE,
                reason = "No other documents share a counterparty name with this one."))
  }

  peers <- db_query(con, sprintf(
    "SELECT document_id, name, value_amount, value_currency FROM instances_Contract
     WHERE status != 'rejected' AND value_amount IS NOT NULL AND document_id IN (%s)",
    paste(rep("?", length(other_docs)), collapse = ", ")), as.list(other_docs))
  if (nrow(peers) == 0) {
    return(list(available = FALSE,
                reason = "Related documents have no extracted contract value to compare."))
  }

  # Currencies are compared only within a currency: converting them would need
  # a rate for the right date, which is not something to invent here.
  same_ccy <- peers[!is.na(peers$value_currency) &
                    peers$value_currency == (this$value_currency %||% ""), , drop = FALSE]
  mixed <- nrow(same_ccy) < nrow(peers)
  values <- suppressWarnings(as.numeric(same_ccy$value_amount))
  values <- values[!is.na(values)]
  mine   <- suppressWarnings(as.numeric(this$value_amount))

  if (length(values) == 0) {
    return(list(available = FALSE,
                reason = "Related contracts are in other currencies; no conversion is applied."))
  }

  list(
    available          = TRUE,
    this_value         = mine,
    currency           = this$value_currency,
    peer_count         = length(values),
    peer_median        = stats::median(values),
    peer_max           = max(values),
    peer_min           = min(values),
    ratio_to_median    = if (stats::median(values) > 0) round(mine / stats::median(values), 2) else NA,
    mixed_currencies_excluded = mixed
  )
}

#' @keywords internal
corpus_system_prompt <- function() {
  paste0(
    "You are advising a public servant comparing one contract against others in ",
    "their corpus. You are given best-effort matches made by normalising raw name ",
    "text -- not resolved entities.\n\n",
    "Return JSON:\n",
    '{"summary":"...","observations":["..."],"suggested_checks":["..."]}\n\n',
    "Rules:\n",
    "- Treat every match as provisional. Say 'a company with this name' rather than asserting identity.\n",
    "- Where name spellings differ under one match, call that out as needing confirmation.\n",
    "- Do not assert a conflict of interest. Suggest what a person should check."
  )
}
