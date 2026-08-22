# ---------------------------------------------------------------------------
# Pipeline steps 4 and 5: population, local and cloud.
#
# Both tiers write through the same persistence path, so a cloud-extracted
# instance is indistinguishable from a local one except in its `source` column
# -- which is the point: review, audit and analysis need one shape, not two.
# ---------------------------------------------------------------------------

#' @keywords internal
population_system_prompt <- function(bundle) {
  types <- vapply(bundle$object_types %||% list(), function(ot) {
    props <- vapply(ot$properties %||% list(), function(p) {
      if (p$id %in% c("instance_id", "document_id", "source", "confidence",
                      "status", "amended_by", "amended_at", "naive_key")) return("")
      # Where a codelist governs a property, the model is asked to classify into
      # it rather than to paraphrase. Without this the model writes "Direct
      # Award" and every concept written against the codelist misses it.
      allowed <- unlist(p$values %||% list(), use.names = FALSE)
      sprintf("      %s (%s): %s%s", p$id, p$type %||% "string", p$description %||% "",
              if (length(allowed))
                paste0("\n        MUST be exactly one of: ", paste(allowed, collapse = ", "))
              else "")
    }, character(1))
    props <- props[nzchar(props)]
    sprintf("  %s -- %s\n%s", ot$id, ot$description %||% "", paste(props, collapse = "\n"))
  }, character(1))

  links <- vapply(bundle$link_types %||% list(), function(lt) {
    sprintf("  %s: %s -> %s (%s)", lt$id, lt$from, lt$to, lt$description %||% "")
  }, character(1))

  paste0(
    "You extract structured instances from public-sector contract documents.\n\n",
    "OBJECT TYPES\n", paste(types, collapse = "\n"), "\n\n",
    "LINK TYPES\n", paste(links, collapse = "\n"), "\n\n",
    "Return JSON:\n",
    '{"entities":[{"temp_id":"e1","type_id":"Contract","confidence":0.9,',
    '"excerpt":"verbatim supporting text","properties":{"name":"..."}}],\n',
    ' "relationships":[{"link_type_id":"party_to","from_temp_id":"e2",',
    '"to_temp_id":"e1","confidence":0.9,"evidence":"verbatim supporting text"}]}\n\n',
    "Rules:\n",
    "- Every entity needs an excerpt quoted verbatim from the document. Without one it cannot be reviewed.\n",
    "- Keep the page marker in the excerpt where the text has one, so the finding can be traced to a page.\n",
    "- Use only the listed type and link ids. If something important has no home in the schema, still\n",
    "  include the property -- unrecognised properties are reviewed, not discarded.\n",
    "- Never invent a value that is not supported by the text. Omit the property instead.\n",
    "- confidence must be one of 1.0, 0.9, 0.7, 0.5, 0.2:\n",
    "    1.0 stated explicitly and unambiguously\n",
    "    0.9 clearly named with its attributes present\n",
    "    0.7 mentioned, with structure implied\n",
    "    0.5 inferred from context\n",
    "    0.2 speculative\n"
  )
}

#' Run an extraction pass over a document
#'
#' The local tier always runs the deterministic date and amount pass first,
#' then a local model over the whole document. The cloud tier is opt-in, sends
#' excerpts rather than the whole document by default, and is recorded in the
#' cloud audit log.
#'
#' @param con A writable connection.
#' @param document_id Document identifier.
#' @param tier `"local"` or `"cloud"`.
#' @param actor_id Actor triggering the run.
#' @param opt_in Explicit cloud opt-in. Required for `tier = "cloud"`.
#' @param deterministic Run the regex date/amount pass (local tier only).
#' @param force Re-run a tier that has already succeeded for this document.
#'   Instances the earlier run produced that no one has reviewed are superseded;
#'   anything a human confirmed or amended is left alone.
#' @return A list summarising what was written.
#' @export
orph_extract <- function(con, document_id, tier = c("local", "cloud"),
                         actor_id = NULL, opt_in = FALSE, deterministic = TRUE,
                         force = FALSE) {
  assert_writable(con)
  tier <- match.arg(tier)
  doc <- orph_get_document(con, document_id)
  if (is.null(doc)) cli::cli_abort("No document {.val {document_id}}.")

  # Re-running a tier would otherwise write a second copy of every instance,
  # leaving a reviewer to work out which of two identical rows is current.
  prior <- db_get_one(con,
    "SELECT run_id FROM extraction_runs
     WHERE document_id = ? AND tier = ? AND status = 'succeeded' LIMIT 1",
    list(document_id, tier))
  if (!is.null(prior) && !isTRUE(force)) {
    cli::cli_abort(c(
      "The {tier} tier has already run on {.val {document_id}}.",
      i = "Re-running would write a second copy of every instance.",
      i = "Pass {.code force = TRUE} to supersede the unreviewed results of the earlier run."
    ))
  }
  if (!is.null(prior) && isTRUE(force)) {
    superseded <- supersede_tier_instances(con, document_id, source_label_for(tier), actor_id)
    if (superseded > 0) {
      cli::cli_alert_info("Superseded {superseded} unreviewed instance(s) from the previous {tier} run.")
    }
  }

  bundle <- orph_active_bundle(con)
  if (is.null(bundle)) cli::cli_abort("No active ontology bundle. Register one first.")

  if (tier == "cloud") orph_assert_cloud_allowed(con, opt_in = opt_in, actor_id = actor_id)

  source_label <- source_label_for(tier)
  run_id <- orph_id("run")
  db_insert(con, "extraction_runs", list(
    run_id = run_id, document_id = document_id, tier = tier, actor_id = nullable(actor_id),
    bundle_id = bundle$bundle_id, bundle_version = bundle$version,
    started_at = orph_now(), status = "running"))

  result <- tryCatch({
    n_det <- 0L
    if (tier == "local" && deterministic) {
      n_det <- run_deterministic_pass(con, document_id, bundle, actor_id)
    }

    if (tier == "cloud") {
      send_mode <- orph_setting(con, "cloud_send_mode", "excerpt")
      if (identical(send_mode, "full_document")) {
        payload <- orph_document_text(con, document_id); excerpt_only <- FALSE
      } else {
        terms <- c("indemn", "liabilit", "terminat", "renew", "confidential",
                   "payment", "party", "parties", "agreement", "signed", "warrant")
        sel <- orph_select_excerpts(con, document_id, terms)
        payload <- sel$text; excerpt_only <- sel$excerpt_only
      }
    } else {
      payload <- orph_document_text(con, document_id); excerpt_only <- FALSE
    }

    if (!orph_document_has_text(con, document_id)) {
      cli::cli_abort(c("Document {.val {document_id}} has no text to extract from.",
                       i = "Its pages may need OCR -- check {.field text_source} on document_pages."))
    }

    # Record the call ourselves: the population engine owns the model call, so
    # nothing else would write the cloud audit row.
    record_llm_call(con, tier, "populate", document_id, actor_id,
                    payload = payload, excerpt_only = excerpt_only)

    src <- build_discovery_source(con, document_id, text = payload)
    src$system_prompt <- population_system_prompt(bundle)
    pop <- orph_populate(bundle, src, orph_llm_fn(tier), tier = tier)

    written <- persist_population(con, document_id, bundle, pop, source_label, actor_id)

    DBI::dbExecute(con,
      "UPDATE extraction_runs SET finished_at = ?, status = 'succeeded',
         n_entities = ?, n_edges = ?, n_amendments = ? WHERE run_id = ?",
      params = list(orph_now(), written$n_entities + n_det, written$n_edges,
                    written$n_amendments, run_id))

    c(written, list(run_id = run_id, tier = tier, document_id = document_id,
                    n_deterministic = n_det, excerpt_only = excerpt_only))
  }, error = function(e) {
    DBI::dbExecute(con,
      "UPDATE extraction_runs SET finished_at = ?, status = 'failed', error = ? WHERE run_id = ?",
      params = list(orph_now(), conditionMessage(e), run_id))
    cli::cli_abort(c("Extraction failed for {.val {document_id}}.", x = conditionMessage(e)))
  })

  result
}

#' @keywords internal
source_label_for <- function(tier) if (identical(tier, "local")) "ai_local" else "ai_cloud"

#' Retire the unreviewed output of an earlier run of the same tier
#'
#' Rejected rather than deleted, in keeping with the amendment model: the
#' superseded rows stay queryable as evidence about extraction quality. Rows a
#' human confirmed or amended are untouched -- a re-run must never discard a
#' person's decision.
#'
#' @keywords internal
supersede_tier_instances <- function(con, document_id, source_label, actor_id) {
  bundle <- orph_active_bundle(con)
  n <- 0L
  with_tx(con, {
    for (ot in managed_object_types(bundle)) {
      if (!DBI::dbExistsTable(con, ot$table_name)) next
      stale <- db_query(con, sprintf(
        "SELECT instance_id FROM %s
         WHERE document_id = ? AND source = ? AND status = 'unconfirmed'",
        DBI::dbQuoteIdentifier(con, ot$table_name)), list(document_id, source_label))
      if (nrow(stale) == 0) next
      DBI::dbExecute(con, sprintf(
        "UPDATE %s SET status = 'rejected', amended_at = ?
         WHERE document_id = ? AND source = ? AND status = 'unconfirmed'",
        DBI::dbQuoteIdentifier(con, ot$table_name)),
        params = list(orph_now(), document_id, source_label))
      for (id in stale$instance_id) {
        record_edit(con, ot$table_name, id, document_id, "superseded",
                    previous = list(status = "unconfirmed"),
                    new = list(status = "rejected"), actor_id = actor_id,
                    note = "Superseded by a later extraction run of the same tier.")
        mark_dependent_evaluations_stale(con, id, "A later extraction run superseded this instance.")
        n <- n + 1L
      }
    }
    DBI::dbExecute(con,
      "UPDATE edges SET status = 'rejected', amended_at = ?
       WHERE document_id = ? AND source = ? AND status = 'unconfirmed'",
      params = list(orph_now(), document_id, source_label))
  })
  n
}

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

#' Columns every instance table carries that are not model output
#' @keywords internal
RESERVED_PROPS <- c("instance_id", "document_id", "source", "confidence", "status",
                    "amended_by", "amended_at", "created_at")

#' Write one instance
#'
#' Properties the bundle does not declare are not dropped: each becomes a
#' schema amendment candidate. Silently discarding them would lose exactly the
#' signal that tells you the schema is wrong.
#'
#' @keywords internal
insert_instance <- function(con, bundle, type_id, instance_id, document_id, properties,
                            source, confidence, status = "unconfirmed", actor_id = NULL) {
  ot <- orph_object_type(bundle, type_id)
  if (is.null(ot)) {
    record_schema_amendment(con, document_id, "new_type", type_id, NULL,
                            observed_value = as.character(to_json(properties)),
                            rationale = sprintf("Extraction produced unknown object type '%s'.", type_id),
                            actor_id = actor_id)
    return(NULL)
  }

  declared <- orph_property_ids(ot)
  values <- list(instance_id = instance_id, document_id = document_id,
                 source = source, confidence = confidence, status = status,
                 created_at = orph_now())

  for (nm in names(properties %||% list())) {
    val <- properties[[nm]]
    if (is.null(val) || length(val) == 0) next
    if (nm %in% RESERVED_PROPS) next
    if (nm %in% declared) {
      values[[nm]] <- if (length(val) > 1) paste(as.character(val), collapse = "; ") else val[[1]]
    } else {
      record_schema_amendment(con, document_id, "new_property", type_id, nm,
                              observed_value = paste(as.character(val), collapse = "; "),
                              rationale = "Property seen during population but not declared in the bundle.",
                              actor_id = actor_id)
    }
  }

  # naive_key is derived, never model output: it must stay a pure function of
  # `name`, or cross-document matching silently varies by extraction run.
  if ("naive_key" %in% declared && !is.null(values$name)) {
    values$naive_key <- orph_naive_key(values$name)
  }

  db_insert(con, ot$table_name, values)
  db_insert(con, "instance_index", list(
    instance_id = instance_id, type_id = type_id, table_name = ot$table_name,
    document_id = document_id, created_at = orph_now()))
  invisible(instance_id)
}

#' @keywords internal
persist_population <- function(con, document_id, bundle, pop, source_label, actor_id) {
  n_entities <- 0L; n_edges <- 0L
  id_map <- character()

  with_tx(con, {
    for (e in pop$entities) {
      new_id <- orph_id("inst")
      written <- insert_instance(con, bundle, e$type_id, new_id, document_id,
                                 e$properties, source_label, e$confidence,
                                 actor_id = actor_id)
      if (is.null(written)) next
      id_map[[e$instance_id]] <- new_id
      n_entities <- n_entities + 1L

      db_insert(con, "provenance", list(
        provenance_id = orph_id("prov"), instance_id = new_id, document_id = document_id,
        source_label  = e$source_label %||% "", page_no = nullable(e$page_no),
        excerpt = e$excerpt %||% "", confidence = e$confidence,
        source = source_label, created_at = orph_now()))

      record_edit(con, orph_object_type(bundle, e$type_id)$table_name, new_id, document_id,
                  "extract", previous = NULL,
                  new = list(type_id = e$type_id, properties = e$properties,
                             source = source_label, confidence = e$confidence),
                  actor_id = actor_id)
    }

    for (r in pop$relationships) {
      from <- id_map[[r$from_instance_id]] %||% NA_character_
      to   <- id_map[[r$to_instance_id]]   %||% NA_character_
      if (is.na(from) || is.na(to)) next
      if (is.null(orph_link_type(bundle, r$link_type_id))) {
        record_schema_amendment(con, document_id, "new_link_type", NULL, r$link_type_id,
                                observed_value = r$evidence %||% "",
                                rationale = "Link type seen during population but not declared in the bundle.",
                                actor_id = actor_id)
        next
      }
      edge_id <- orph_id("edge")
      db_insert(con, "edges", list(
        edge_id = edge_id, from_instance_id = from, to_instance_id = to,
        link_type_id = r$link_type_id, document_id = document_id,
        evidence = r$evidence %||% "", source = source_label,
        confidence = r$confidence, status = "unconfirmed", created_at = orph_now()))
      record_edit(con, "edges", edge_id, document_id, "extract", previous = NULL,
                  new = list(link_type_id = r$link_type_id, from = from, to = to,
                             confidence = r$confidence), actor_id = actor_id)
      n_edges <- n_edges + 1L
    }

    for (a in pop$amendments) {
      record_schema_amendment(con, document_id, a$amendment_type, a$type_id, a$property_id,
                              observed_value = a$observed_value, inferred_type = a$inferred_type,
                              rationale = a$rationale, actor_id = actor_id)
    }

    # Backfill the containment key on deterministic findings now that a
    # Contract exists for this document.
    link_deterministic_to_contract(con, document_id)
  })

  n_amend <- db_get_one(con,
    "SELECT COUNT(*) AS n FROM schema_amendments WHERE document_id = ? AND status = 'pending'",
    list(document_id))$n

  list(n_entities = n_entities, n_edges = n_edges,
       n_amendments = as.integer(n_amend %||% 0L),
       dropped_edges = pop$dropped_edges %||% 0L)
}

#' @keywords internal
link_deterministic_to_contract <- function(con, document_id) {
  contract <- db_get_one(con,
    "SELECT instance_id FROM instances_Contract
     WHERE document_id = ? AND status != 'rejected' ORDER BY confidence DESC LIMIT 1",
    list(document_id))
  if (is.null(contract)) return(invisible(FALSE))
  for (tbl in c("instances_KeyDate", "instances_MonetaryAmount", "instances_Clause")) {
    if (!DBI::dbExistsTable(con, tbl)) next
    DBI::dbExecute(con, sprintf(
      "UPDATE %s SET contract_instance_id = ?
       WHERE document_id = ? AND (contract_instance_id IS NULL OR contract_instance_id = '')",
      DBI::dbQuoteIdentifier(con, tbl)),
      params = list(contract$instance_id, document_id))
  }
  invisible(TRUE)
}

#' Has this deterministic finding already been recorded for this document?
#'
#' The deterministic pass commits in its own transaction, before the model pass
#' runs. That is deliberate -- a pattern-matched date is worth keeping even if
#' the model call then fails -- but it means a retry after a failure would write
#' the same findings a second time. The guard in orph_extract() does not catch
#' it, because that only refuses a tier that already *succeeded*.
#'
#' Findings are therefore matched on what they are: the same raw text on the
#' same page of the same document is the same finding. A row a reviewer already
#' rejected does not block a fresh one, so a deliberate re-run still refreshes.
#'
#' @keywords internal
deterministic_finding_exists <- function(con, table_name, document_id, raw_text, page_no) {
  if (!DBI::dbExistsTable(con, table_name)) return(FALSE)
  hit <- db_get_one(con, sprintf(
    "SELECT instance_id FROM %s
     WHERE document_id = ? AND raw_text = ? AND page_no = ? AND status != 'rejected'
     LIMIT 1", DBI::dbQuoteIdentifier(con, table_name)),
    list(document_id, raw_text, page_no))
  !is.null(hit)
}

#' @keywords internal
run_deterministic_pass <- function(con, document_id, bundle, actor_id) {
  pages <- db_query(con,
    "SELECT page_no, text FROM document_pages WHERE document_id = ? ORDER BY page_no",
    list(document_id))
  if (nrow(pages) == 0) return(0L)

  n <- 0L
  with_tx(con, {
    for (i in seq_len(nrow(pages))) {
      page_no <- pages$page_no[[i]]
      text    <- pages$text[[i]] %||% ""
      if (!nzchar(trimws(text))) next

      dates <- orph_find_dates(text)
      for (j in seq_len(nrow(dates))) {
        if (deterministic_finding_exists(con, "instances_KeyDate", document_id,
                                         dates$raw_text[[j]], page_no)) next
        pos  <- regexpr(dates$raw_text[[j]], text, fixed = TRUE)
        role <- infer_role(text, pos, DATE_ROLE_CUES)
        id   <- orph_id("inst")
        insert_instance(con, bundle, "KeyDate", id, document_id,
                        list(value = dates$value[[j]], raw_text = dates$raw_text[[j]],
                             date_role = role, page_no = page_no),
                        "ai_local", dates$confidence[[j]], actor_id = actor_id)
        db_insert(con, "provenance", list(
          provenance_id = orph_id("prov"), instance_id = id, document_id = document_id,
          source_label = "deterministic:date", source = "ai_local", page_no = page_no,
          excerpt = excerpt_around(text, pos, dates$raw_text[[j]]),
          confidence = dates$confidence[[j]], created_at = orph_now()))
        record_edit(con, "instances_KeyDate", id, document_id, "extract", NULL,
                    list(value = dates$value[[j]], date_role = role, source = "ai_local"),
                    actor_id, note = if (isTRUE(dates$ambiguous[[j]]))
                      "Day/month order is ambiguous; recorded day-first." else NULL)
        n <- n + 1L
      }

      amounts <- orph_find_amounts(text)
      for (j in seq_len(nrow(amounts))) {
        if (deterministic_finding_exists(con, "instances_MonetaryAmount", document_id,
                                         amounts$raw_text[[j]], page_no)) next
        pos  <- regexpr(amounts$raw_text[[j]], text, fixed = TRUE)
        role <- infer_role(text, pos, AMOUNT_ROLE_CUES)
        id   <- orph_id("inst")
        insert_instance(con, bundle, "MonetaryAmount", id, document_id,
                        list(amount = amounts$amount[[j]], currency = amounts$currency[[j]],
                             raw_text = amounts$raw_text[[j]], role = role, page_no = page_no),
                        "ai_local", amounts$confidence[[j]], actor_id = actor_id)
        db_insert(con, "provenance", list(
          provenance_id = orph_id("prov"), instance_id = id, document_id = document_id,
          source_label = "deterministic:amount", source = "ai_local", page_no = page_no,
          excerpt = excerpt_around(text, pos, amounts$raw_text[[j]]),
          confidence = amounts$confidence[[j]], created_at = orph_now()))
        record_edit(con, "instances_MonetaryAmount", id, document_id, "extract", NULL,
                    list(amount = amounts$amount[[j]], currency = amounts$currency[[j]],
                         role = role, source = "ai_local"), actor_id)
        n <- n + 1L
      }
    }
  })
  n
}

#' @keywords internal
excerpt_around <- function(text, pos, needle, window = 120L) {
  if (is.na(pos) || pos < 1) return(needle)
  start <- max(1, pos - window)
  end   <- min(nchar(text), pos + nchar(needle) + window)
  trimws(substr(text, start, end))
}

#' Instances extracted from a document
#'
#' @param con A connection.
#' @param document_id Document identifier.
#' @param type_id Restrict to one object type, or `NULL` for all.
#' @param include_rejected Include rejected rows.
#' @return A data frame with one row per instance and its provenance.
#' @export
orph_document_instances <- function(con, document_id, type_id = NULL,
                                    include_rejected = FALSE) {
  sql <- "SELECT i.instance_id, i.type_id, i.table_name, p.excerpt, p.page_no,
                 p.source_label, p.confidence AS provenance_confidence
          FROM instance_index i
          LEFT JOIN provenance p ON p.instance_id = i.instance_id
          WHERE i.document_id = ?"
  params <- list(document_id)
  if (!is.null(type_id)) { sql <- paste(sql, "AND i.type_id = ?"); params <- c(params, type_id) }
  idx <- db_query(con, paste(sql, "ORDER BY i.type_id, i.created_at"), params)
  if (nrow(idx) == 0) return(idx)

  # Property values live in per-type tables, so they are fetched per type and
  # attached as JSON rather than forced into one wide, mostly-empty frame.
  props <- vapply(seq_len(nrow(idx)), function(k) {
    row <- db_get_one(con, sprintf("SELECT * FROM %s WHERE instance_id = ?",
                                   DBI::dbQuoteIdentifier(con, idx$table_name[[k]])),
                      list(idx$instance_id[[k]]))
    if (is.null(row)) return(NA_character_)
    as.character(to_json(row[!names(row) %in% c("instance_id", "table_name")]))
  }, character(1))
  idx$properties <- props

  status <- vapply(seq_len(nrow(idx)), function(k) {
    row <- db_get_one(con, sprintf("SELECT status FROM %s WHERE instance_id = ?",
                                   DBI::dbQuoteIdentifier(con, idx$table_name[[k]])),
                      list(idx$instance_id[[k]]))
    row$status %||% NA_character_
  }, character(1))
  idx$status <- status

  if (!include_rejected) idx <- idx[!idx$status %in% ORPH_EXCLUDED_STATUSES, , drop = FALSE]
  idx
}
