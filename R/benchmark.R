# ---------------------------------------------------------------------------
# Benchmarking extraction against an externally labelled corpus.
#
# Phase 1's quality measurement derives from human corrections, which means it
# says nothing until people have reviewed a meaningful number of documents.
# A labelled corpus short-circuits that: CUAD carries 13,000 expert-labelled
# clause spans across 510 commercial contracts, so extraction can be scored
# today rather than after a review cycle.
#
# What this is not: CUAD is US commercial contracts. Irish public-sector
# procurement documents differ in structure, vocabulary and what matters in
# them. A good CUAD score is evidence the extraction machinery works, not
# evidence it works on the documents this platform is for. The report says so
# rather than leaving it to be assumed.
# ---------------------------------------------------------------------------

#' Path to the shipped CUAD category mapping
#' @export
orph_cuad_map_path <- function() {
  p <- system.file("benchmarks", "cuad-clause-map.json", package = "orpheus")
  if (nzchar(p)) return(p)
  file.path("inst", "benchmarks", "cuad-clause-map.json")
}

#' Load a CUAD-format labelled corpus
#'
#' CUAD ships in SQuAD form: `data[].title`, `paragraphs[].context`, and
#' `qas[]` whose `question` names the clause category and whose `answers[]`
#' carry `text` and `answer_start`.
#'
#' The category vocabulary is read **from the file**, never hardcoded. CUAD
#' documents 41 categories; a filtered or extended copy will have a different
#' set, and a benchmark that silently scored against the wrong vocabulary would
#' be worse than none.
#'
#' @param path Path to a CUAD JSON file (e.g. `CUADv1.json`).
#' @return A list with `contracts` (title, context) and `labels` (one row per
#'   labelled span) and `categories`.
#' @export
orph_load_cuad <- function(path) {
  if (!file.exists(path)) cli::cli_abort("No CUAD file at {.path {path}}.")
  raw <- jsonlite::fromJSON(path, simplifyVector = FALSE)
  entries <- raw$data %||% raw
  if (length(entries) == 0) cli::cli_abort("{.path {path}} contains no entries.")

  contracts <- list(); labels <- list()
  for (entry in entries) {
    title <- entry$title %||% orph_id("cuad")
    for (para in entry$paragraphs %||% list()) {
      context <- para$context %||% ""
      contracts[[length(contracts) + 1L]] <- data.frame(
        title = title, context = context, stringsAsFactors = FALSE)

      for (qa in para$qas %||% list()) {
        category <- cuad_category(qa$question %||% "")
        answers <- qa$answers %||% list()
        # is_impossible / no answers means the category is absent from this
        # contract. That is a real label -- a true negative -- and dropping it
        # would make precision unmeasurable.
        if (length(answers) == 0) {
          labels[[length(labels) + 1L]] <- data.frame(
            title = title, category = category, text = NA_character_,
            answer_start = NA_integer_, present = FALSE, stringsAsFactors = FALSE)
          next
        }
        for (ans in answers) {
          labels[[length(labels) + 1L]] <- data.frame(
            title = title, category = category,
            text = ans$text %||% "",
            answer_start = as.integer(ans$answer_start %||% NA),
            present = TRUE, stringsAsFactors = FALSE)
        }
      }
    }
  }

  contracts <- do.call(rbind, contracts)
  labels    <- if (length(labels)) do.call(rbind, labels) else data.frame()
  list(contracts = contracts, labels = labels,
       categories = sort(unique(labels$category)))
}

#' Recover a category name from a CUAD question
#'
#' CUAD questions are long natural-language prompts that name the category in
#' quotes, e.g. `Highlight the parts ... related to "Governing Law" ...`.
#'
#' @keywords internal
cuad_category <- function(question) {
  quoted <- regmatches(question, regexpr('"[^"]+"', question))
  if (length(quoted) && nzchar(quoted[[1]])) return(gsub('"', "", quoted[[1]]))
  trimws(question)
}

#' Load the CUAD category to clause_type mapping
#'
#' Seeded, not complete. The mapping is a data file rather than code precisely
#' so it can be extended without a release, and
#' [orph_benchmark_extraction()] reports unmapped categories rather than
#' scoring them silently as misses.
#'
#' @param path Path to the mapping JSON.
#' @return A named character vector: CUAD category to `clause_type`.
#' @export
orph_load_cuad_map <- function(path = orph_cuad_map_path()) {
  if (!file.exists(path)) cli::cli_abort("No CUAD mapping at {.path {path}}.")
  m <- jsonlite::fromJSON(path, simplifyVector = FALSE)
  entries <- m$mapping %||% list()
  stats::setNames(vapply(entries, function(x) x %||% NA_character_, character(1)),
                  names(entries))
}

#' Score one document's extraction against CUAD labels
#'
#' A labelled span counts as found when an extracted `Clause` of the mapped
#' `clause_type` contains it, or is contained by it. Exact string equality
#' would be the wrong test: CUAD spans are lawyer-selected excerpts and an
#' extractor legitimately returns a longer or shorter run of the same clause.
#'
#' @param con A connection.
#' @param document_id Document identifier.
#' @param labels Labels for this contract, from [orph_load_cuad()].
#' @param mapping CUAD category to `clause_type`.
#' @return A list with per-category counts.
#' @keywords internal
score_document_against_cuad <- function(con, document_id, labels, mapping) {
  extracted <- db_query(con,
    "SELECT clause_type, text FROM instances_Clause
     WHERE document_id = ? AND status != 'rejected'", list(document_id))

  norm <- function(x) tolower(gsub("\\s+", " ", trimws(x %||% "")))
  extracted$norm <- norm(extracted$text)

  rows <- list()
  for (category in unique(labels$category)) {
    # Named-vector lookup with a missing name errors rather than returning NULL,
    # so an unmapped category has to be tested for explicitly. Getting this
    # wrong turned a reportable gap in the benchmark's configuration into a
    # crash midway through a run.
    mapped <- if (category %in% names(mapping)) unname(mapping[[category]]) else NA_character_
    cat_labels <- labels[labels$category == category & labels$present, , drop = FALSE]

    if (length(mapped) == 0 || is.na(mapped)) {
      rows[[length(rows) + 1L]] <- data.frame(
        category = category, clause_type = NA_character_,
        n_labelled = nrow(cat_labels), n_found = NA_integer_,
        n_extracted = NA_integer_, mapped = FALSE, stringsAsFactors = FALSE)
      next
    }

    candidates <- extracted$norm[extracted$clause_type == mapped]
    found <- 0L
    for (i in seq_len(nrow(cat_labels))) {
      span <- norm(cat_labels$text[[i]])
      if (!nzchar(span)) next
      hit <- any(vapply(candidates, function(c)
        nzchar(c) && (grepl(span, c, fixed = TRUE) || grepl(c, span, fixed = TRUE)),
        logical(1)))
      if (hit) found <- found + 1L
    }

    rows[[length(rows) + 1L]] <- data.frame(
      category = category, clause_type = mapped,
      n_labelled = nrow(cat_labels), n_found = found,
      n_extracted = length(candidates), mapped = TRUE, stringsAsFactors = FALSE)
  }
  if (length(rows) == 0) return(data.frame())
  do.call(rbind, rows)
}

#' Benchmark extraction against a CUAD corpus
#'
#' Ingests each contract's text as a document, runs the configured extraction
#' tier over it, and scores the extracted clauses against CUAD's labelled
#' spans.
#'
#' @param con A writable connection.
#' @param cuad Output of [orph_load_cuad()].
#' @param tier `"local"` or `"cloud"`.
#' @param limit Maximum contracts to run. CUAD is 510 contracts; a full cloud
#'   run is not free, so this defaults to a small sample.
#' @param actor_id Actor to attribute the run to.
#' @param opt_in Cloud opt-in, required for `tier = "cloud"`.
#' @param storage_root Where ingested copies are written.
#' @param mapping CUAD category to `clause_type`.
#' @return A list with per-category results, headline recall, and caveats.
#' @export
orph_benchmark_extraction <- function(con, cuad, tier = c("local", "cloud"),
                                      limit = 5L, actor_id = NULL, opt_in = FALSE,
                                      storage_root = "storage/benchmark",
                                      mapping = orph_load_cuad_map()) {
  assert_writable(con)
  tier <- match.arg(tier)
  contracts <- utils::head(cuad$contracts, limit)
  if (nrow(contracts) == 0) cli::cli_abort("No contracts to benchmark.")

  unmapped <- setdiff(cuad$categories, names(mapping)[!is.na(mapping)])
  per_contract <- list()

  for (i in seq_len(nrow(contracts))) {
    title <- contracts$title[[i]]
    path <- file.path(tempdir(), paste0(orph_id("bench"), ".txt"))
    writeLines(contracts$context[[i]], path)
    on.exit(unlink(path), add = TRUE)

    ing <- orph_ingest(con, path, actor_id = actor_id, storage_root = storage_root,
                       filename = paste0(gsub("[^A-Za-z0-9]+", "-", title), ".txt"))
    if (isTRUE(ing$duplicate)) next

    ok <- tryCatch({
      orph_extract(con, ing$document_id, tier = tier, actor_id = actor_id,
                   opt_in = opt_in)
      TRUE
    }, error = function(e) {
      cli::cli_warn("Extraction failed for {.val {title}}: {conditionMessage(e)}")
      FALSE
    })
    if (!ok) next

    scored <- score_document_against_cuad(
      con, ing$document_id, cuad$labels[cuad$labels$title == title, , drop = FALSE],
      mapping)
    if (nrow(scored)) {
      scored$title <- title
      per_contract[[length(per_contract) + 1L]] <- scored
    }
  }

  if (length(per_contract) == 0) {
    return(list(n_contracts = 0L, by_category = data.frame(),
                note = "No contract was extracted successfully."))
  }

  all_rows <- do.call(rbind, per_contract)
  mapped_rows <- all_rows[all_rows$mapped, , drop = FALSE]

  by_category <- do.call(rbind, lapply(split(mapped_rows, mapped_rows$category), function(part) {
    labelled <- sum(part$n_labelled)
    data.frame(
      category    = part$category[[1]],
      clause_type = part$clause_type[[1]],
      n_labelled  = labelled,
      n_found     = sum(part$n_found),
      recall      = if (labelled == 0) NA_real_ else round(sum(part$n_found) / labelled, 3),
      stringsAsFactors = FALSE)
  }))
  rownames(by_category) <- NULL
  by_category <- by_category[order(by_category$recall, na.last = TRUE), , drop = FALSE]

  total_labelled <- sum(by_category$n_labelled)
  list(
    n_contracts        = length(unique(all_rows$title)),
    tier               = tier,
    by_category        = by_category,
    overall_recall     = if (total_labelled == 0) NA_real_
                         else round(sum(by_category$n_found) / total_labelled, 3),
    unmapped_categories = unmapped,
    caveats = c(
      "Recall only. CUAD labels which clauses exist, not which do not, so a clause extracted where CUAD has no label is unjudgeable rather than wrong.",
      "CUAD is US commercial contracts. A good score is evidence the extraction machinery works, not that it works on Irish public-sector procurement documents.",
      if (length(unmapped) > 0)
        sprintf("%d CUAD categor%s have no clause_type mapping and were not scored.",
                length(unmapped), if (length(unmapped) == 1) "y" else "ies")
    )
  )
}
