# ---------------------------------------------------------------------------
# Pipeline step 7: document-level analysis.
#
# Two things wear the name "analysis" and they are kept apart, because they
# have different failure modes and want different review:
#
#   rule concepts  -- conceptR: versioned SQL boolean expressions evaluated per
#                     row. Deterministic, reproducible, diffable between
#                     versions, and cheap. "Is this contract high value?"
#   narrative      -- a model reading the extracted instances and writing a
#                     summary, risk level, issues and recommendations. Not
#                     reproducible, and the part that most needs a human.
#
# Both land in concept_evaluations with the same provenance and amendment
# fields, distinguished by `kind`, and both record which instances they read so
# that amending one of those instances marks the evaluation stale.
# ---------------------------------------------------------------------------

#' @keywords internal
require_conceptr <- function() {
  if (!requireNamespace("conceptR", quietly = TRUE)) {
    cli::cli_abort(c(
      "{.pkg conceptR} is not installed.",
      i = "Install it with {.code remotes::install_github('CathalByrneGit/conceptR')}."
    ))
  }
}

#' Build a conceptR context over the store
#'
#' conceptR requires a bundle carrying the `ontology_bundle` class; the bundle
#' read back out of SQLite is a plain list, so the class is restored here
#' rather than being stored in the JSON where it would not survive a round trip.
#'
#' @param con A connection.
#' @param bundle A bundle. Defaults to the active one.
#' @return A `ConceptContext`.
#' @export
orph_concept_context <- function(con, bundle = NULL) {
  require_conceptr()
  bundle <- bundle %||% orph_active_bundle(con)
  if (is.null(bundle)) cli::cli_abort("No active ontology bundle.")
  class(bundle) <- c("ontology_bundle", "list")
  conceptR::concept_context(bundle, con)
}

#' Register the bundle's seed concepts with conceptR
#'
#' Idempotent: a concept already defined keeps its version history, and a
#' changed expression is added as a new version rather than overwriting the old
#' one, so an evaluation made under the previous version stays explainable.
#'
#' @param con A writable connection.
#' @param bundle A bundle. Defaults to the active one.
#' @param actor_id Actor registering them.
#' @return A data frame of concept ids and the version now active.
#' @export
orph_setup_concepts <- function(con, bundle = NULL, actor_id = NULL) {
  assert_writable(con)
  bundle <- bundle %||% orph_active_bundle(con)
  ctx <- orph_concept_context(con, bundle)

  out <- list()
  for (cd in bundle$concept_defs %||% list()) {
    scope <- cd$scope %||% "default"
    defined <- db_query(con, "SELECT concept_id FROM concept_definitions WHERE concept_id = ?",
                        list(cd$id))
    if (nrow(defined) == 0) {
      conceptR::cpt_define(ctx, cd$id, cd$object_type_id, cd$description %||% "")
    }

    versions <- db_query(con,
      "SELECT version, sql_expr, status FROM concept_versions
       WHERE concept_id = ? AND scope = ? ORDER BY version", list(cd$id, scope))
    current <- versions[versions$status == "active", , drop = FALSE]

    if (nrow(current) > 0 && identical(trimws(current$sql_expr[[1]]), trimws(cd$sql_expr))) {
      out[[length(out) + 1L]] <- data.frame(concept_id = cd$id, scope = scope,
                                            version = current$version[[1]], action = "unchanged",
                                            stringsAsFactors = FALSE)
      next
    }

    v <- conceptR::cpt_add_version(ctx, cd$id, scope, cd$sql_expr, status = "draft",
                                   rationale = cd$rationale %||% NULL)
    suppressWarnings(conceptR::cpt_activate(ctx, cd$id, scope, v))
    # A superseded version is deprecated rather than deleted: evaluations made
    # under it keep pointing at a version that still exists.
    if (nrow(current) > 0) {
      conceptR::cpt_deprecate(ctx, cd$id, scope, current$version[[1]])
    }
    record_edit(con, "concept_versions", paste0(cd$id, "/", scope, "/", v), NULL,
                "concept_version_added", previous = NULL,
                new = list(concept_id = cd$id, scope = scope, version = v,
                           sql_expr = cd$sql_expr), actor_id = actor_id)
    out[[length(out) + 1L]] <- data.frame(concept_id = cd$id, scope = scope, version = v,
                                          action = if (nrow(current) > 0) "new_version" else "created",
                                          stringsAsFactors = FALSE)
  }
  if (length(out) == 0) return(data.frame())
  do.call(rbind, out)
}

#' Evaluate the rule concepts for a document
#'
#' Runs every active concept whose object type has instances in this document.
#' A concept that comes out true raises a `Flag` instance, so a rule finding
#' sits alongside model-raised flags in the same review queue rather than in a
#' parallel one.
#'
#' @param con A writable connection.
#' @param document_id Document identifier.
#' @param actor_id Actor triggering the evaluation.
#' @return A data frame of concept results for this document.
#' @export
orph_evaluate_concepts <- function(con, document_id, actor_id = NULL) {
  assert_writable(con)
  bundle <- orph_active_bundle(con)
  ctx    <- orph_concept_context(con, bundle)

  defs <- db_query(con,
    "SELECT d.concept_id, d.object_type_id, v.scope, v.version
     FROM concept_definitions d
     JOIN concept_versions v ON v.concept_id = d.concept_id
     WHERE v.status = 'active'")
  if (nrow(defs) == 0) return(data.frame())

  results <- list()
  for (i in seq_len(nrow(defs))) {
    cid <- defs$concept_id[[i]]; ot_id <- defs$object_type_id[[i]]
    scope <- defs$scope[[i]];    version <- defs$version[[i]]
    ot <- orph_object_type(bundle, ot_id)
    if (is.null(ot) || !DBI::dbExistsTable(con, ot$table_name)) next

    # conceptR evaluates the whole table, so the document filter and the
    # exclusion of rejected rows happen here. Rejected rows must not raise
    # flags: a fact a reviewer threw out should not come back as a finding.
    live <- db_query(con, sprintf(
      "SELECT instance_id FROM %s WHERE document_id = ? AND status != 'rejected'",
      DBI::dbQuoteIdentifier(con, ot$table_name)), list(document_id))
    if (nrow(live) == 0) next

    evaluated <- tryCatch(
      conceptR::cpt_evaluate(ctx, cid, scope = scope, version = version, object_type_id = ot_id),
      error = function(e) {
        cli::cli_warn("Concept {.val {cid}} failed to evaluate: {conditionMessage(e)}")
        NULL
      })
    if (is.null(evaluated)) next

    pk <- ot$primary_key
    evaluated <- evaluated[evaluated[[pk]] %in% live$instance_id, , drop = FALSE]
    if (nrow(evaluated) == 0) next

    hits <- evaluated[!is.na(evaluated[[cid]]) & evaluated[[cid]], , drop = FALSE]
    results[[length(results) + 1L]] <- data.frame(
      concept_id = cid, scope = scope, version = version, object_type_id = ot_id,
      n_evaluated = nrow(evaluated), n_true = nrow(hits), stringsAsFactors = FALSE)

    for (instance_id in hits[[pk]]) {
      write_concept_evaluation(con, bundle,
        concept_id = cid, concept_version = version, concept_scope = scope,
        kind = "rule", scope_level = "document", document_id = document_id,
        result = list(concept_id = cid, object_type_id = ot_id,
                      instance_id = instance_id, value = TRUE),
        dependencies = instance_id, source = "ai_local",
        confidence = unname(ORPH_CONFIDENCE[["explicit"]]), actor_id = actor_id)

      raise_flag(con, bundle, document_id, instance_id, cid, actor_id)
    }
  }

  if (length(results) == 0) return(data.frame())
  do.call(rbind, results)
}

#' @keywords internal
raise_flag <- function(con, bundle, document_id, target_instance_id, concept_id, actor_id) {
  existing <- db_get_one(con,
    "SELECT instance_id FROM instances_Flag
     WHERE document_id = ? AND target_instance_id = ? AND flag_type = ? AND status != 'rejected'",
    list(document_id, target_instance_id, concept_id))
  if (!is.null(existing)) return(invisible(existing$instance_id))

  id <- orph_id("inst")
  insert_instance(con, bundle, "Flag", id, document_id,
                  list(target_instance_id = target_instance_id, flag_type = concept_id,
                       severity = "medium",
                       rationale = sprintf("Rule concept '%s' evaluated true.", concept_id),
                       raised_by_pass = "concept"),
                  "ai_local", unname(ORPH_CONFIDENCE[["explicit"]]), actor_id = actor_id)
  record_edit(con, "instances_Flag", id, document_id, "extract", NULL,
              list(flag_type = concept_id, target = target_instance_id), actor_id)
  invisible(id)
}

#' @keywords internal
write_concept_evaluation <- function(con, bundle, concept_id, concept_version, concept_scope,
                                     kind, scope_level, document_id, result, dependencies,
                                     source, confidence, actor_id,
                                     corpus_context = NULL, resolution_quality = NULL) {
  id <- orph_id("eval")
  db_insert(con, "concept_evaluations", list(
    evaluation_id = id, concept_id = concept_id, concept_version = concept_version,
    concept_scope = nullable(concept_scope), kind = kind, scope = scope_level,
    target_document_id = nullable(document_id),
    result = as.character(to_json(result)),
    corpus_context_used = if (is.null(corpus_context)) NA_character_
                          else as.character(to_json(corpus_context)),
    resolution_quality = nullable(resolution_quality),
    source = source, confidence = confidence, status = "unconfirmed",
    generated_at = orph_now(), generated_by = nullable(actor_id), stale = 0L))

  for (dep in unique(dependencies %||% character())) {
    if (is.na(dep) || !nzchar(dep)) next
    DBI::dbExecute(con,
      "INSERT OR IGNORE INTO concept_evaluation_dependencies (evaluation_id, instance_id)
       VALUES (?, ?)", params = list(id, dep))
  }
  record_edit(con, "concept_evaluations", id, document_id, "evaluate", NULL,
              list(concept_id = concept_id, kind = kind, scope = scope_level),
              actor_id = actor_id)
  invisible(id)
}

# ---------------------------------------------------------------------------
# Narrative analysis
# ---------------------------------------------------------------------------

#' @keywords internal
NARRATIVE_CONCEPT_ID <- "document_narrative"

#' @keywords internal
narrative_system_prompt <- function() {
  paste0(
    "You are advising a public servant reviewing a contract. You are given the ",
    "structured facts already extracted from one document, with the confidence ",
    "and review status of each.\n\n",
    "Return JSON:\n",
    '{"summary":"...","risk_level":"low|medium|high",',
    '"key_issues":[{"issue":"...","severity":"low|medium|high","basis":"which extracted fact this rests on"}],',
    '"recommendations":["..."],"confidence":0.7}\n\n',
    "Rules:\n",
    "- Reason only from the facts given. Do not introduce terms that are not there.\n",
    "- Where a fact is unconfirmed or low confidence, say so in the issue rather than relying on it.\n",
    "- If the extracted facts are too thin to support a judgement, say that in the summary\n",
    "  and return risk_level 'low' with an empty key_issues list.\n",
    "- confidence must be one of 1.0, 0.9, 0.7, 0.5, 0.2."
  )
}

#' Generate the narrative analysis for a document
#'
#' Interpretation, not extraction: a summary, risk level, key issues and
#' recommendations built from the instances already extracted. It reads
#' structured facts rather than the document text, which keeps what is sent to
#' a cloud model to the facts a person has already been able to see.
#'
#' @param con A writable connection.
#' @param document_id Document identifier.
#' @param tier `"cloud"` (the default; this is the nuanced-reading pass) or `"local"`.
#' @param actor_id Actor triggering it.
#' @param opt_in Explicit cloud opt-in. Required when `tier = "cloud"`.
#' @return A list with the evaluation id and the parsed result.
#' @export
orph_analyse_document <- function(con, document_id, tier = c("cloud", "local"),
                                  actor_id = NULL, opt_in = FALSE) {
  assert_writable(con)
  tier <- match.arg(tier)
  if (tier == "cloud") orph_assert_cloud_allowed(con, opt_in = opt_in, actor_id = actor_id)

  bundle <- orph_active_bundle(con)
  facts  <- collect_document_facts(con, bundle, document_id)
  # Checked against the instance ids, not against facts$instances -- that is a
  # two-element wrapper (instances, relationships) and is never empty.
  if (length(facts$instance_ids) == 0) {
    cli::cli_abort(c("Nothing has been extracted from {.val {document_id}} yet.",
                     i = "Run an extraction pass before analysing it."))
  }

  reply <- orph_llm_json(con, tier, narrative_system_prompt(),
                         as.character(to_json(facts$instances)),
                         purpose = "narrative_analysis", document_id = document_id,
                         actor_id = actor_id, excerpt_only = TRUE, opt_in = opt_in)

  result <- list(
    summary         = as_scalar_or_na(reply$summary),
    risk_level      = normalise_risk(reply$risk_level),
    key_issues      = reply$key_issues %||% list(),
    recommendations = reply$recommendations %||% list()
  )
  confidence <- orph_snap_confidence(reply$confidence %||% 0.5)
  source <- if (tier == "cloud") "ai_cloud" else "ai_local"

  eval_id <- NULL
  with_tx(con, {
    # A re-analysis supersedes the previous one rather than sitting beside it,
    # so "the analysis" is never ambiguous.
    DBI::dbExecute(con,
      "UPDATE concept_evaluations SET stale = 1, stale_reason = 'Superseded by a later analysis.'
       WHERE target_document_id = ? AND kind = 'narrative' AND stale = 0",
      params = list(document_id))
    eval_id <- write_concept_evaluation(con, bundle,
      concept_id = NARRATIVE_CONCEPT_ID, concept_version = NA_integer_, concept_scope = NA_character_,
      kind = "narrative", scope_level = "document", document_id = document_id,
      result = result, dependencies = facts$instance_ids, source = source,
      confidence = confidence, actor_id = actor_id)
  })

  list(evaluation_id = eval_id, document_id = document_id, source = source,
       confidence = confidence, confidence_label = orph_confidence_label(confidence),
       status = "unconfirmed", result = result,
       depends_on_instances = length(facts$instance_ids))
}

#' @keywords internal
normalise_risk <- function(x) {
  x <- tolower(as_scalar_or_na(x))
  if (is.na(x) || !(x %in% c("low", "medium", "high"))) "medium" else x
}

#' Collect the extracted facts for a document
#'
#' @keywords internal
collect_document_facts <- function(con, bundle, document_id) {
  instances <- list(); ids <- character()
  for (ot in bundle$object_types %||% list()) {
    if (identical(ot$x_orpheus$managed, FALSE)) next
    if (!DBI::dbExistsTable(con, ot$table_name)) next
    rows <- db_query(con, sprintf(
      "SELECT * FROM %s WHERE document_id = ? AND status != 'rejected'",
      DBI::dbQuoteIdentifier(con, ot$table_name)), list(document_id))
    if (nrow(rows) == 0) next
    for (i in seq_len(nrow(rows))) {
      row <- as.list(rows[i, , drop = FALSE])
      row <- row[!vapply(row, function(v) all(is.na(v)), logical(1))]
      ids <- c(ids, row$instance_id)
      row$type_id <- ot$id
      row$confidence_label <- orph_confidence_label(row$confidence %||% NA)
      instances[[length(instances) + 1L]] <- row
    }
  }
  edges <- db_query(con,
    "SELECT link_type_id, from_instance_id, to_instance_id, evidence, confidence, status
     FROM edges WHERE document_id = ? AND status != 'rejected'", list(document_id))

  list(instances = list(instances = instances,
                        relationships = if (nrow(edges)) edges else list()),
       instance_ids = unique(ids))
}

#' Read the evaluations for a document
#'
#' @param con A connection.
#' @param document_id Document identifier.
#' @param kind `"rule"`, `"narrative"`, or `NULL` for both.
#' @param include_stale Include superseded or stale evaluations.
#' @return A data frame, newest first.
#' @export
orph_document_evaluations <- function(con, document_id, kind = NULL, include_stale = TRUE) {
  sql <- "SELECT * FROM concept_evaluations WHERE target_document_id = ?"
  params <- list(document_id)
  if (!is.null(kind))     { sql <- paste(sql, "AND kind = ?"); params <- c(params, kind) }
  if (!include_stale)     { sql <- paste(sql, "AND stale = 0") }
  db_query(con, paste(sql, "ORDER BY generated_at DESC"), params)
}

#' Review an evaluation
#'
#' The same amendment model as instances: an interpretation is editable, and
#' the original is preserved.
#'
#' @param con A writable connection.
#' @param evaluation_id Evaluation identifier.
#' @param status `"confirmed"`, `"amended"` or `"rejected"`.
#' @param actor_id Actor reviewing.
#' @param result Replacement result (required when `status = "amended"`).
#' @param note Optional free text.
#' @return Invisibly, the evaluation id.
#' @export
orph_review_evaluation <- function(con, evaluation_id,
                                   status = c("confirmed", "amended", "rejected"),
                                   actor_id, result = NULL, note = NULL) {
  assert_writable(con)
  assert_string(actor_id, "actor_id")
  status <- match.arg(status)
  before <- db_get_one(con, "SELECT * FROM concept_evaluations WHERE evaluation_id = ?",
                       list(evaluation_id))
  if (is.null(before)) cli::cli_abort("No evaluation {.val {evaluation_id}}.")
  if (status == "amended" && is.null(result)) {
    cli::cli_abort("{.arg result} is required when amending an evaluation.")
  }

  with_tx(con, {
    if (status == "amended") {
      DBI::dbExecute(con,
        "UPDATE concept_evaluations SET status = ?, result = ?, source = 'human',
           confidence = ?, amended_by = ?, amended_at = ?, stale = 0, stale_reason = NULL
         WHERE evaluation_id = ?",
        params = list(status, as.character(to_json(result)),
                      unname(ORPH_CONFIDENCE[["explicit"]]), actor_id, orph_now(), evaluation_id))
    } else {
      DBI::dbExecute(con,
        "UPDATE concept_evaluations SET status = ?, amended_by = ?, amended_at = ?
         WHERE evaluation_id = ?",
        params = list(status, actor_id, orph_now(), evaluation_id))
    }
    record_edit(con, "concept_evaluations", evaluation_id, before$target_document_id, status,
                previous = list(status = before$status, result = from_json(before$result)),
                new = list(status = status, result = result), actor_id = actor_id, note = note)
  })
  invisible(evaluation_id)
}
