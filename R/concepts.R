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

# ---------------------------------------------------------------------------
# Concept parameters
# ---------------------------------------------------------------------------

#' Setting key holding a deployment's override for a template parameter
#' @keywords internal
concept_param_key <- function(template_id, parameter) {
  paste0("concept_param.", template_id, ".", parameter)
}

#' Effective value of every concept template parameter
#'
#' A threshold like "high value means a million" is a local policy question, not
#' a fact about contracts. The bundle carries a default so the pipeline runs out
#' of the box; a deployment overrides it without editing the bundle. This shows
#' both, and which one is in force.
#'
#' @param con A connection.
#' @param bundle A bundle. Defaults to the active one.
#' @return A data frame of template, parameter, default, effective value and source.
#' @export
orph_concept_parameters <- function(con, bundle = NULL) {
  bundle <- bundle %||% orph_active_bundle(con)
  rows <- list()
  for (tmpl in bundle$concept_templates %||% list()) {
    for (nm in names(tmpl$parameters %||% list())) {
      spec <- tmpl$parameters[[nm]]
      override <- orph_setting(con, concept_param_key(tmpl$template_id, nm), NULL)
      rows[[length(rows) + 1L]] <- data.frame(
        template_id = tmpl$template_id,
        parameter   = nm,
        type        = spec$type %||% "string",
        default     = as.character(spec$default %||% NA),
        effective   = as.character(override %||% spec$default %||% NA),
        source      = if (is.null(override)) "bundle_default" else "deployment_override",
        description = spec$description %||% "",
        stringsAsFactors = FALSE)
    }
  }
  if (length(rows) == 0) return(data.frame())
  do.call(rbind, rows)
}

#' Set a concept template parameter for this deployment
#'
#' Takes effect by adding a **new concept version** rather than editing the
#' current one, so an evaluation made under the old threshold still points at a
#' version that exists and still explains itself. Changing the number is a
#' schema-level act, and it is recorded as one.
#'
#' @param con A writable connection.
#' @param template_id Template identifier.
#' @param parameter Parameter name.
#' @param value New value.
#' @param actor_id Actor making the change.
#' @return Invisibly, the result of re-registering the affected concepts.
#' @export
orph_set_concept_parameter <- function(con, template_id, parameter, value, actor_id) {
  assert_writable(con)
  assert_string(actor_id, "actor_id")
  bundle <- orph_active_bundle(con)

  tmpl <- NULL
  for (candidate in bundle$concept_templates %||% list()) {
    if (identical(candidate$template_id, template_id)) tmpl <- candidate
  }
  if (is.null(tmpl)) cli::cli_abort("No concept template {.val {template_id}} in the bundle.")
  if (!(parameter %in% names(tmpl$parameters %||% list()))) {
    known <- names(tmpl$parameters %||% list())
    cli::cli_abort(c("Template {.val {template_id}} has no parameter {.val {parameter}}.",
                     i = "Its parameters are {.val {known}}."))
  }

  # Formatted before storing, not at render time: the setting is what
  # orph_concept_parameters() shows an administrator, so storing "5e+06" would
  # put scientific notation in front of the person checking the threshold even
  # if the SQL came out fine.
  stored <- format_param(value)
  previous <- orph_setting(con, concept_param_key(template_id, parameter), NULL)
  with_tx(con, {
    orph_set_setting(con, concept_param_key(template_id, parameter), stored, actor_id)
    record_edit(con, "org_settings", concept_param_key(template_id, parameter), NULL,
                "concept_parameter_changed",
                previous = list(value = previous), new = list(value = stored),
                actor_id = actor_id)
  })
  result <- orph_setup_concepts(con, bundle, actor_id = actor_id)
  # A new concept version means any score built on it is pinned to the old one
  # until its components are re-synced.
  if (length(bundle$scores %||% list()) > 0) {
    try(orph_setup_scores(con, bundle, actor_id = actor_id), silent = TRUE)
  }
  invisible(result)
}

#' Resolve a concept definition to SQL, rendering its template if it has one
#' @keywords internal
resolve_concept_sql <- function(con, bundle, cd) {
  if (is.null(cd$template_id)) return(cd$sql_expr)

  tmpl <- NULL
  for (candidate in bundle$concept_templates %||% list()) {
    if (identical(candidate$template_id, cd$template_id)) tmpl <- candidate
  }
  if (is.null(tmpl)) {
    cli::cli_abort("Concept {.val {cd$id}} names unknown template {.val {cd$template_id}}.")
  }

  values <- list()
  for (nm in names(tmpl$parameters %||% list())) {
    spec <- tmpl$parameters[[nm]]
    override <- orph_setting(con, concept_param_key(tmpl$template_id, nm), NULL)
    supplied <- (cd$parameter_values %||% list())[[nm]]
    values[[nm]] <- override %||% supplied %||% spec$default
    if (is.null(values[[nm]])) {
      cli::cli_abort("Parameter {.val {nm}} of template {.val {tmpl$template_id}} has no value.")
    }
  }

  sql <- tmpl$base_sql_expr
  for (nm in names(values)) {
    sql <- gsub(paste0("\\{\\{", nm, "\\}\\}"), format_param(values[[nm]]), sql)
  }
  left <- regmatches(sql, gregexpr("\\{\\{[A-Za-z_][A-Za-z0-9_]*\\}\\}", sql, perl = TRUE))[[1]]
  if (length(left) > 0) {
    cli::cli_abort("Template {.val {tmpl$template_id}} left placeholders unfilled: {.val {left}}")
  }
  sql
}

#' Render a parameter value into SQL text
#'
#' `as.character(5000000)` is `"5e+06"`. SQLite parses that, but the rendered
#' expression is stored, versioned and read by people deciding whether a concept
#' is right -- and a threshold that reads as `5e+06` in the audit trail is a
#' threshold nobody checks.
#'
#' @keywords internal
format_param <- function(x) {
  if (is.numeric(x)) format(x, scientific = FALSE, trim = TRUE)
  else as.character(x)
}

#' Register the bundle's concept templates with conceptR
#' @keywords internal
register_concept_templates <- function(con, bundle, ctx) {
  for (tmpl in bundle$concept_templates %||% list()) {
    existing <- db_query(con, "SELECT template_id FROM concept_templates WHERE template_id = ?",
                         list(tmpl$template_id))
    if (nrow(existing) > 0) next
    params <- lapply(tmpl$parameters %||% list(), function(spec) {
      list(type = spec$type %||% "string", default = spec$default)
    })
    conceptR::cpt_define_template(ctx, tmpl$template_id, tmpl$object_type_id,
                                  tmpl$base_sql_expr, params,
                                  description = tmpl$description %||% "")
  }
  invisible(TRUE)
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
  register_concept_templates(con, bundle, ctx)

  out <- list()
  for (cd in bundle$concept_defs %||% list()) {
    scope <- cd$scope %||% "default"
    sql_expr <- resolve_concept_sql(con, bundle, cd)
    defined <- db_query(con, "SELECT concept_id FROM concept_definitions WHERE concept_id = ?",
                        list(cd$id))
    if (nrow(defined) == 0) {
      conceptR::cpt_define(ctx, cd$id, cd$object_type_id, cd$description %||% "")
    }

    versions <- db_query(con,
      "SELECT version, sql_expr, status FROM concept_versions
       WHERE concept_id = ? AND scope = ? ORDER BY version", list(cd$id, scope))
    current <- versions[versions$status == "active", , drop = FALSE]

    if (nrow(current) > 0 && identical(trimws(current$sql_expr[[1]]), trimws(sql_expr))) {
      out[[length(out) + 1L]] <- data.frame(concept_id = cd$id, scope = scope,
                                            version = current$version[[1]], action = "unchanged",
                                            stringsAsFactors = FALSE)
      next
    }

    v <- conceptR::cpt_add_version(ctx, cd$id, scope, sql_expr, status = "draft",
                                   rationale = cd$rationale %||% NULL,
                                   template_id = cd$template_id %||% NULL,
                                   parameter_values = cd$parameter_values %||% NULL)
    suppressWarnings(conceptR::cpt_activate(ctx, cd$id, scope, v))
    # A superseded version is deprecated rather than deleted: evaluations made
    # under it keep pointing at a version that still exists.
    if (nrow(current) > 0) {
      conceptR::cpt_deprecate(ctx, cd$id, scope, current$version[[1]])
    }
    record_edit(con, "concept_versions", paste0(cd$id, "/", scope, "/", v), NULL,
                "concept_version_added", previous = NULL,
                new = list(concept_id = cd$id, scope = scope, version = v,
                           sql_expr = sql_expr), actor_id = actor_id)
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
# Composite scores
# ---------------------------------------------------------------------------

#' Register the bundle's composite scores with conceptR
#'
#' Idempotent: a score already defined keeps its components rather than being
#' redefined, so a score referenced by past evaluations stays intact.
#'
#' @param con A writable connection.
#' @param bundle A bundle. Defaults to the active one.
#' @param actor_id Actor registering them.
#' @return A data frame of score ids and how many components each has.
#' @export
orph_setup_scores <- function(con, bundle = NULL, actor_id = NULL) {
  assert_writable(con)
  bundle <- bundle %||% orph_active_bundle(con)
  ctx <- orph_concept_context(con, bundle)
  if (length(bundle$scores %||% list()) == 0) return(data.frame())

  out <- list()
  for (sc in bundle$scores) {
    existing <- db_query(con, "SELECT score_id FROM composite_scores WHERE score_id = ?",
                         list(sc$score_id))
    if (nrow(existing) == 0) {
      conceptR::cpt_define_score(ctx, sc$score_id, sc$object_type_id,
                                 components = list(),
                                 aggregation = sc$aggregation %||% "weighted_sum",
                                 thresholds = sc$thresholds %||% NULL,
                                 description = sc$description %||% "")
      record_edit(con, "composite_scores", sc$score_id, NULL, "score_defined", NULL,
                  list(score_id = sc$score_id, thresholds = sc$thresholds), actor_id)
    }
    for (comp in sc$components %||% list()) {
      # conceptR documents version = NULL as "use whichever is active at
      # evaluation time", but composite_score_components.version is NOT NULL
      # and part of the primary key, so that path always fails. The active
      # version is therefore resolved and pinned here.
      #
      # That turns out to be the better behaviour anyway: the score records
      # which concept version it scored against, so a result stays explainable
      # after a threshold changes. The cost is that components must be re-synced
      # when a concept gains a version, which is what the delete below does.
      active <- db_get_one(con,
        "SELECT version FROM concept_versions
         WHERE concept_id = ? AND scope = ? AND status = 'active'
         ORDER BY version DESC LIMIT 1", list(comp$concept_id, comp$scope))
      if (is.null(active)) {
        cli::cli_warn(paste0("Score '", sc$score_id, "' references concept '",
                             comp$concept_id, "' which has no active version; skipping."))
        next
      }

      DBI::dbExecute(con,
        "DELETE FROM composite_score_components
         WHERE score_id = ? AND concept_id = ? AND scope = ? AND version != ?",
        params = list(sc$score_id, comp$concept_id, comp$scope, active$version))

      have <- db_query(con,
        "SELECT 1 FROM composite_score_components
         WHERE score_id = ? AND concept_id = ? AND scope = ? AND version = ?",
        list(sc$score_id, comp$concept_id, comp$scope, active$version))
      if (nrow(have) > 0) next

      conceptR::cpt_add_score_component(ctx, sc$score_id, comp$concept_id, comp$scope,
                                        version = active$version,
                                        weight = comp$weight %||% 1)
    }
    n <- db_get_one(con, "SELECT COUNT(*) AS n FROM composite_score_components WHERE score_id = ?",
                    list(sc$score_id))$n
    out[[length(out) + 1L]] <- data.frame(score_id = sc$score_id,
                                          n_components = as.integer(n),
                                          stringsAsFactors = FALSE)
  }
  do.call(rbind, out)
}

#' Evaluate a composite score for a document
#'
#' Arithmetic over concepts that have already been evaluated: reproducible,
#' explainable, and diffable between versions — everything the narrative risk
#' level is not. The two are meant to coexist. Where they disagree is the
#' interesting case, and [orph_risk_comparison()] surfaces it.
#'
#' @param con A writable connection.
#' @param document_id Document identifier.
#' @param score_id Score to evaluate. Defaults to the bundle's first.
#' @param actor_id Actor triggering it.
#' @return A list with the score, its tier, and which components fired.
#' @export
orph_evaluate_score <- function(con, document_id, score_id = NULL, actor_id = NULL) {
  assert_writable(con)
  bundle <- orph_active_bundle(con)
  scores <- bundle$scores %||% list()
  if (length(scores) == 0) cli::cli_abort("The active bundle defines no composite scores.")

  score_def <- if (is.null(score_id)) scores[[1]] else {
    hit <- Filter(function(s) identical(s$score_id, score_id), scores)
    if (length(hit) == 0) cli::cli_abort("No score {.val {score_id}} in the bundle.")
    hit[[1]]
  }

  ctx <- orph_concept_context(con, bundle)
  ot  <- orph_object_type(bundle, score_def$object_type_id)
  pk  <- ot$primary_key

  live <- db_query(con, sprintf(
    "SELECT instance_id FROM %s WHERE document_id = ? AND status != 'rejected'",
    DBI::dbQuoteIdentifier(con, ot$table_name)), list(document_id))
  if (nrow(live) == 0) {
    cli::cli_abort(c("No {.val {score_def$object_type_id}} instance in {.val {document_id}}.",
                     i = "Run an extraction pass first."))
  }

  scored <- conceptR::cpt_evaluate_score(ctx, score_def$score_id)
  scored <- scored[scored[[pk]] %in% live$instance_id, , drop = FALSE]
  if (nrow(scored) == 0) return(list(score_id = score_def$score_id, results = list()))

  weights <- stats::setNames(
    vapply(score_def$components, function(c) as.numeric(c$weight %||% 1), numeric(1)),
    vapply(score_def$components, function(c) c$concept_id, character(1)))

  results <- list()
  with_tx(con, {
    for (i in seq_len(nrow(scored))) {
      row <- scored[i, , drop = FALSE]
      instance_id <- row[[pk]]

      fired <- names(weights)[vapply(names(weights), function(cid) {
        isTRUE(as.logical(row[[cid]] %||% FALSE))
      }, logical(1))]

      result <- list(
        score_id   = score_def$score_id,
        instance_id = instance_id,
        score      = as.numeric(row$score %||% NA),
        tier       = as.character(row$tier %||% NA),
        thresholds = score_def$thresholds,
        # Which concepts contributed, and by how much. A score nobody can
        # decompose is no better than the model's opinion.
        contributions = lapply(fired, function(cid)
          list(concept_id = cid, weight = unname(weights[[cid]]))),
        max_possible = sum(weights))

      write_concept_evaluation(con, bundle,
        concept_id = score_def$score_id, concept_version = NA_integer_,
        concept_scope = NA_character_, kind = "score", scope_level = "document",
        document_id = document_id, result = result, dependencies = instance_id,
        source = "ai_local", confidence = unname(ORPH_CONFIDENCE[["explicit"]]),
        actor_id = actor_id)

      results[[length(results) + 1L]] <- result
    }
  })

  list(score_id = score_def$score_id, document_id = document_id, results = results)
}

#' Compare the deterministic score against the narrative risk level
#'
#' Two independent readings of the same document. Agreement is mild evidence
#' both are working; disagreement is the useful signal, and says nothing about
#' which one is wrong.
#'
#' @param con A connection.
#' @param document_id Document identifier.
#' @return A list with both readings and whether they agree.
#' @export
orph_risk_comparison <- function(con, document_id) {
  score <- db_get_one(con,
    "SELECT result FROM concept_evaluations
     WHERE target_document_id = ? AND kind = 'score' AND stale = 0
     ORDER BY generated_at DESC LIMIT 1", list(document_id))
  narrative <- db_get_one(con,
    "SELECT result FROM concept_evaluations
     WHERE target_document_id = ? AND kind = 'narrative' AND stale = 0
     ORDER BY generated_at DESC LIMIT 1", list(document_id))

  score_tier <- if (is.null(score)) NA_character_ else from_json(score$result)$tier
  narrative_level <- if (is.null(narrative)) NA_character_ else from_json(narrative$result)$risk_level

  list(
    document_id       = document_id,
    score_tier        = score_tier %||% NA_character_,
    narrative_level   = narrative_level %||% NA_character_,
    available         = !is.na(score_tier) && !is.na(narrative_level),
    agree             = if (is.na(score_tier) || is.na(narrative_level)) NA
                        else identical(tolower(score_tier), tolower(narrative_level)),
    note = if (is.na(score_tier) || is.na(narrative_level))
      "Both a score and a narrative analysis are needed before they can be compared."
    else if (identical(tolower(score_tier), tolower(narrative_level)))
      "The rule-based score and the model's reading agree."
    else
      paste("The rule-based score and the model's reading disagree. Neither is",
            "authoritative -- the score can only see concepts that were evaluated,",
            "and the model can only see the facts that were extracted.")
  )
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
