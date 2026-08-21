# ---------------------------------------------------------------------------
# Extraction quality measurement.
#
# Phase 1's stated goal is extraction good enough to trust as a foundation.
# The store already holds what is needed to judge that -- every correction
# preserves the machine's value beside the human's -- but holding the evidence
# and reporting on it are different things, and only the second one answers
# "is this good enough yet".
#
# The measurement is exhaustive rather than sampled: every reviewed row is a
# labelled example, so there is nothing to sample from. What it must never do
# is treat unreviewed rows as evidence. An unconfirmed instance is not a
# correct one; it is an unknown one. Every figure here is therefore computed
# over the reviewed subset and reported alongside the coverage it rests on.
# ---------------------------------------------------------------------------

#' Statuses that count as a human having judged a row
#' @keywords internal
REVIEWED_STATUSES <- c("confirmed", "amended", "rejected")

#' @keywords internal
quality_scope_clause <- function(document_id) {
  if (is.null(document_id)) list(sql = "", params = list())
  else list(sql = " AND document_id = ?", params = list(document_id))
}

#' Gather review outcomes, keyed to what the machine originally said
#'
#' Deliberately does not read `confidence` or `source` off the instance row.
#' Amending an instance sets both to the human's values -- confidence 1.0,
#' source `human` -- because after a correction the row *is* ground truth. That
#' is right for every downstream use and wrong for this one: grouping by the
#' post-correction confidence would report that every amended fact was
#' extracted at full confidence, which inverts the very thing being measured.
#'
#' The original values come from `provenance`, which is written once at
#' extraction and never amended.
#'
#' @param con A connection.
#' @param document_id Restrict to one document, or `NULL` for the whole corpus.
#' @return A data frame of one row per instance, reviewed and unreviewed.
#' @keywords internal
collect_review_outcomes <- function(con, document_id = NULL) {
  bundle <- orph_active_bundle(con)
  if (is.null(bundle)) cli::cli_abort("No active ontology bundle.")
  scope <- quality_scope_clause(document_id)

  frames <- list()
  for (ot in managed_object_types(bundle)) {
    if (!DBI::dbExistsTable(con, ot$table_name)) next

    # Flags raised by a rule concept are excluded. Their confidence records
    # that a SQL expression evaluated true, which is always `explicit` and says
    # nothing about how well anything was extracted -- so a coarse rule whose
    # flags get dismissed would drag the extraction figures down and, worse,
    # make the rubric look inverted when it is fine. Rule quality is a separate
    # question, answered by orph_concept_precision(). Flags a model raised are
    # extraction output and do count.
    rule_filter <- if (identical(ot$id, "Flag")) " AND i.raised_by_pass != 'concept'" else ""

    df <- db_query(con, sprintf(
      "SELECT i.instance_id, i.document_id, i.status,
              p.confidence AS confidence, p.source AS source
       FROM %s i
       LEFT JOIN provenance p ON p.instance_id = i.instance_id
       WHERE 1 = 1%s%s",
      DBI::dbQuoteIdentifier(con, ot$table_name), rule_filter,
      gsub("document_id", "i.document_id", scope$sql, fixed = TRUE)),
      scope$params)
    if (nrow(df) == 0) next

    # An instance with no provenance row cannot be attributed, so it is left
    # out of the rates rather than silently counted at an invented confidence.
    df <- df[!is.na(df$confidence), , drop = FALSE]
    if (nrow(df) == 0) next

    df$source <- ifelse(is.na(df$source), "unknown", df$source)
    df$type_id <- ot$id
    frames[[length(frames) + 1L]] <- df
  }
  if (length(frames) == 0) {
    return(data.frame(instance_id = character(), document_id = character(),
                      status = character(), confidence = numeric(),
                      source = character(), type_id = character(),
                      stringsAsFactors = FALSE))
  }
  do.call(rbind, frames)
}

#' @keywords internal
summarise_outcomes <- function(df, by) {
  if (nrow(df) == 0) return(data.frame())
  keys <- interaction(df[by], drop = TRUE, sep = "\r")
  out <- lapply(split(df, keys), function(part) {
    reviewed <- part[part$status %in% REVIEWED_STATUSES, , drop = FALSE]
    row <- part[1, by, drop = FALSE]
    n_rev <- nrow(reviewed)
    cbind(row, data.frame(
      n_total     = nrow(part),
      n_reviewed  = n_rev,
      n_confirmed = sum(reviewed$status == "confirmed"),
      n_amended   = sum(reviewed$status == "amended"),
      n_rejected  = sum(reviewed$status == "rejected"),
      coverage    = round(n_rev / nrow(part), 3),
      # Accepted as extracted: the machine needed no correction at all.
      accuracy    = if (n_rev == 0) NA_real_ else round(sum(reviewed$status == "confirmed") / n_rev, 3),
      # Salvage rate: wrong in detail but the row itself was worth keeping.
      amend_rate  = if (n_rev == 0) NA_real_ else round(sum(reviewed$status == "amended") / n_rev, 3),
      reject_rate = if (n_rev == 0) NA_real_ else round(sum(reviewed$status == "rejected") / n_rev, 3),
      stringsAsFactors = FALSE))
  })
  res <- do.call(rbind, out)
  rownames(res) <- NULL
  res
}

#' Measure extraction quality
#'
#' Reports how often extracted instances survived human review, broken down by
#' object type, confidence level and model tier. Every rate is over reviewed
#' rows only, with `coverage` saying how much of the population that is.
#'
#' @param con A connection.
#' @param document_id Restrict to one document, or `NULL` for the whole corpus.
#' @param min_reviewed Suppress rates for groups with fewer reviewed rows than
#'   this, since a rate computed from three rows is noise wearing a number's
#'   clothes.
#' @return A list of data frames: `overall`, `by_type`, `by_confidence`, `by_tier`.
#' @export
orph_extraction_quality <- function(con, document_id = NULL, min_reviewed = 5L) {
  df <- collect_review_outcomes(con, document_id)
  if (nrow(df) == 0) {
    return(list(overall = data.frame(n_total = 0L, n_reviewed = 0L, coverage = NA_real_,
                                     accuracy = NA_real_),
                by_type = data.frame(), by_confidence = data.frame(), by_tier = data.frame(),
                note = "Nothing has been extracted yet."))
  }
  df$all <- "all"
  df$confidence_label <- orph_confidence_label(df$confidence)

  blank_small <- function(x) {
    if (nrow(x) == 0) return(x)
    small <- !is.na(x$n_reviewed) & x$n_reviewed < min_reviewed
    x$accuracy[small] <- NA_real_
    x$amend_rate[small] <- NA_real_
    x$reject_rate[small] <- NA_real_
    x
  }

  overall <- summarise_outcomes(df, "all")
  overall$all <- NULL

  by_conf <- summarise_outcomes(df, c("confidence", "confidence_label"))
  if (nrow(by_conf)) by_conf <- by_conf[order(-by_conf$confidence), , drop = FALSE]

  list(
    overall       = overall,
    by_type       = blank_small(summarise_outcomes(df, "type_id")),
    by_confidence = blank_small(by_conf),
    by_tier       = blank_small(summarise_outcomes(df, "source")),
    min_reviewed  = min_reviewed
  )
}

#' Check whether the confidence rubric holds up
#'
#' The rubric is only worth carrying if a higher level really does mean a more
#' reliable fact. This compares accuracy across levels and says plainly whether
#' the ordering survives contact with real review -- a rubric that does not rank
#' correctly is worse than no rubric, because people trust it.
#'
#' @param con A connection.
#' @param document_id Restrict to one document, or `NULL` for the corpus.
#' @param min_reviewed Minimum reviewed rows for a level to count.
#' @return A list with the per-level table, a verdict, and any inversions found.
#' @export
orph_confidence_calibration <- function(con, document_id = NULL, min_reviewed = 5L) {
  by_conf <- orph_extraction_quality(con, document_id, min_reviewed)$by_confidence
  usable <- if (nrow(by_conf) == 0) by_conf else by_conf[!is.na(by_conf$accuracy), , drop = FALSE]

  if (nrow(usable) < 2) {
    return(list(levels = by_conf, verdict = "insufficient_evidence", inversions = data.frame(),
                note = sprintf(
                  "Fewer than two confidence levels have %d or more reviewed instances. Review more before trusting the rubric.",
                  min_reviewed)))
  }

  usable <- usable[order(-usable$confidence), , drop = FALSE]
  inversions <- list()
  for (i in seq_len(nrow(usable) - 1L)) {
    if (usable$accuracy[[i]] < usable$accuracy[[i + 1L]]) {
      inversions[[length(inversions) + 1L]] <- data.frame(
        higher_level = usable$confidence_label[[i]],
        higher_accuracy = usable$accuracy[[i]],
        lower_level = usable$confidence_label[[i + 1L]],
        lower_accuracy = usable$accuracy[[i + 1L]],
        stringsAsFactors = FALSE)
    }
  }
  inversions <- if (length(inversions)) do.call(rbind, inversions) else data.frame()

  list(
    levels     = usable,
    verdict    = if (nrow(inversions) == 0) "monotonic" else "inverted",
    inversions = inversions,
    note = if (nrow(inversions) == 0)
      "Accuracy rises with the rubric level, as it should."
    else
      paste("A higher rubric level scored worse than a lower one. The rubric is",
            "not ranking reliability here -- treat the levels as labels, not as",
            "a ranking, until this resolves.")
  )
}

#' Precision of the rule concepts
#'
#' Every rule concept that fires raises a `Flag`. Once a person has reviewed
#' those flags, the fraction confirmed is that concept's precision: how often it
#' was pointing at something real. A coarse concept that over-selects shows up
#' here as a low number, which is the signal to tighten its expression.
#'
#' This does not measure recall. Nothing in the store knows about the issues a
#' concept failed to raise, and pretending otherwise would be worse than the
#' gap.
#'
#' @param con A connection.
#' @param document_id Restrict to one document, or `NULL` for the corpus.
#' @param min_reviewed Minimum reviewed flags for a precision to be reported.
#' @return A data frame, least precise first.
#' @export
orph_concept_precision <- function(con, document_id = NULL, min_reviewed = 5L) {
  if (!DBI::dbExistsTable(con, "instances_Flag")) return(data.frame())
  scope <- quality_scope_clause(document_id)
  flags <- db_query(con, sprintf(
    "SELECT flag_type, status, raised_by_pass FROM instances_Flag
     WHERE raised_by_pass = 'concept'%s", scope$sql), scope$params)
  if (nrow(flags) == 0) return(data.frame())

  out <- lapply(split(flags, flags$flag_type), function(part) {
    reviewed <- part[part$status %in% REVIEWED_STATUSES, , drop = FALSE]
    n_rev <- nrow(reviewed)
    upheld <- sum(reviewed$status %in% c("confirmed", "amended"))
    data.frame(
      concept_id  = part$flag_type[[1]],
      n_raised    = nrow(part),
      n_reviewed  = n_rev,
      n_upheld    = upheld,
      n_dismissed = sum(reviewed$status == "rejected"),
      precision   = if (n_rev < min_reviewed) NA_real_ else round(upheld / n_rev, 3),
      stringsAsFactors = FALSE)
  })
  res <- do.call(rbind, out)
  rownames(res) <- NULL
  res[order(res$precision, na.last = TRUE), , drop = FALSE]
}

#' Which properties get corrected most
#'
#' Read from `edit_history` rather than from the instance tables, because the
#' question is not what a value is now but which fields a person had to change.
#' The properties at the top are where extraction is weakest, and are the ones
#' worth changing a prompt or a pattern for.
#'
#' @param con A connection.
#' @param document_id Restrict to one document, or `NULL` for the corpus.
#' @param limit Maximum rows.
#' @return A data frame with a worked example per property.
#' @export
orph_property_corrections <- function(con, document_id = NULL, limit = 25L) {
  scope <- quality_scope_clause(document_id)
  edits <- db_query(con, sprintf(
    "SELECT table_name, previous_value, new_value FROM edit_history
     WHERE action = 'amend'%s ORDER BY seq", scope$sql), scope$params)
  if (nrow(edits) == 0) return(data.frame())

  rows <- list()
  for (i in seq_len(nrow(edits))) {
    new <- from_json(edits$new_value[[i]])
    old <- from_json(edits$previous_value[[i]])
    for (prop in names(new %||% list())) {
      rows[[length(rows) + 1L]] <- data.frame(
        table_name = edits$table_name[[i]],
        property   = prop,
        was        = truncate_value(old[[prop]]),
        became     = truncate_value(new[[prop]]),
        stringsAsFactors = FALSE)
    }
  }
  if (length(rows) == 0) return(data.frame())
  all_rows <- do.call(rbind, rows)

  out <- lapply(split(all_rows, interaction(all_rows$table_name, all_rows$property,
                                            drop = TRUE, sep = "\r")), function(part) {
    data.frame(
      table_name      = part$table_name[[1]],
      property        = part$property[[1]],
      n_corrections   = nrow(part),
      example_was     = part$was[[1]],
      example_became  = part$became[[1]],
      stringsAsFactors = FALSE)
  })
  res <- do.call(rbind, out)
  rownames(res) <- NULL
  res <- res[order(-res$n_corrections), , drop = FALSE]
  utils::head(res, limit)
}

#' @keywords internal
truncate_value <- function(x, n = 60L) {
  if (is.null(x) || length(x) == 0) return(NA_character_)
  s <- paste(as.character(x), collapse = "; ")
  if (nchar(s) > n) paste0(substr(s, 1, n - 3), "...") else s
}

#' A single extraction-quality report
#'
#' Everything the Phase 1 question needs in one object: is extraction good
#' enough to build on, and where is it weakest.
#'
#' @param con A connection.
#' @param document_id Restrict to one document, or `NULL` for the corpus.
#' @param min_reviewed Minimum reviewed rows before a rate is reported.
#' @return A named list.
#' @export
orph_quality_report <- function(con, document_id = NULL, min_reviewed = 5L) {
  quality <- orph_extraction_quality(con, document_id, min_reviewed)
  overall <- quality$overall

  readiness <- if (!nrow(overall) || is.na(overall$coverage[[1]]) || overall$n_reviewed[[1]] == 0) {
    list(state = "unmeasured",
         note = "No instance has been reviewed, so extraction quality is unknown -- not good, not bad.")
  } else if (overall$coverage[[1]] < 0.2) {
    list(state = "insufficient_review",
         note = sprintf("Only %.0f%% of instances have been reviewed. Too little to judge extraction on.",
                        100 * overall$coverage[[1]]))
  } else {
    list(state = "measured",
         note = sprintf("%.0f%% of instances reviewed; %.0f%% were accepted exactly as extracted.",
                        100 * overall$coverage[[1]], 100 * overall$accuracy[[1]]))
  }

  list(
    scope                 = if (is.null(document_id)) "corpus" else document_id,
    generated_at          = orph_now(),
    readiness             = readiness,
    overall               = overall,
    by_type               = quality$by_type,
    by_confidence         = quality$by_confidence,
    by_tier               = quality$by_tier,
    calibration           = orph_confidence_calibration(con, document_id, min_reviewed),
    concept_precision     = orph_concept_precision(con, document_id, min_reviewed),
    property_corrections  = orph_property_corrections(con, document_id),
    caveat = paste(
      "Rates are computed over reviewed rows only. An unconfirmed instance is",
      "an unknown one, not a correct one, and is excluded from every rate here.",
      "Flags raised by rule concepts are excluded too -- they measure the rules,",
      "not the extraction, and are reported under concept_precision instead.")
  )
}
