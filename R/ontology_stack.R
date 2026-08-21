# ---------------------------------------------------------------------------
# Adapter over ontologyDiscoverR's population flow.
#
# This is the single point of contact with the extraction engine. Everything
# downstream works on the normalised shape returned by orph_populate(), so the
# engine can be swapped -- for a test double, or for a later version of the
# stack -- without touching persistence, review or analysis.
#
# Two things are deliberately not done the obvious way:
#
#  1. The DiscoverySource is built from text already in the store, not by
#     calling pop_add_file(). pop_add_file() re-parses the file from disk with
#     pdftools, which would silently discard OCR text for scanned documents --
#     exactly the documents where extraction quality matters most. Feeding it
#     the page text Orpheus already holds keeps the OCR pass meaningful and
#     avoids parsing every document twice.
#
#  2. Statuses and confidences are translated here rather than stored raw.
#     ontologyDiscoverR uses pending/approved and arbitrary float confidences;
#     this platform uses the four-state review vocabulary and the five-level
#     rubric. Translating at the boundary keeps one vocabulary in the store.
# ---------------------------------------------------------------------------

#' Install a population engine
#'
#' @param fn A function `f(bundle, source, llm_fn, tier)` returning a list with
#'   `entities`, `relationships` and `amendments`, or `NULL` to restore the
#'   ontologyDiscoverR default. Used for tests and for alternative engines.
#' @return Invisibly, the previous engine.
#' @export
orph_set_populator <- function(fn) {
  previous <- orph_env$populator
  if (!is.null(fn) && !is.function(fn)) cli::cli_abort("{.arg fn} must be a function or NULL.")
  orph_env$populator <- fn
  invisible(previous)
}

#' Build a DiscoverySource from stored page text
#'
#' Mirrors the object `ontologyDiscoverR`'s parsers produce, so `pop_extract()`
#' consumes it exactly as if it had parsed the file itself.
#'
#' @param con A connection.
#' @param document_id Document identifier.
#' @param text Text to use. Defaults to the document's stored text.
#' @return A `DiscoverySource`-shaped list.
#' @keywords internal
build_discovery_source <- function(con, document_id, text = NULL) {
  doc <- orph_get_document(con, document_id)
  if (is.null(doc)) cli::cli_abort("No document {.val {document_id}}.")
  structure(
    list(
      source_id    = document_id,
      source_type  = "pdf",
      source_label = doc$filename,
      raw_text     = text %||% orph_document_text(con, document_id),
      structured   = NULL,
      metadata     = list(path = doc$storage_path, n_pages = doc$n_pages,
                          document_id = document_id)
    ),
    class = c("DiscoverySource", "list")
  )
}

#' Run the population pass over one source
#'
#' @param bundle The active ontology bundle.
#' @param source A `DiscoverySource`-shaped list.
#' @param llm_fn A provider function `f(system_prompt)`.
#' @param tier `"local"` or `"cloud"`, passed to a custom engine.
#' @return A list with `entities`, `relationships`, `amendments`, all normalised.
#' @export
orph_populate <- function(bundle, source, llm_fn, tier = "local") {
  engine <- orph_env$populator
  raw <- if (!is.null(engine)) {
    engine(bundle, source, llm_fn, tier)
  } else {
    populate_via_ontologydiscoverr(bundle, source, llm_fn)
  }
  normalise_population(raw)
}

#' @keywords internal
populate_via_ontologydiscoverr <- function(bundle, source, llm_fn) {
  if (!requireNamespace("ontologyDiscoverR", quietly = TRUE)) {
    cli::cli_abort(c(
      "{.pkg ontologyDiscoverR} is not installed, and it is the extraction engine.",
      i = "Install it with
           {.code remotes::install_github('CathalByrneGit/ontologyDiscoverR')}.",
      i = "Or register an alternative engine with {.fn orph_set_populator}."
    ))
  }
  sess <- ontologyDiscoverR::populate_session(bundle, llm = llm_fn, label = source$source_label)
  # Injected directly rather than via pop_add_file(): see the note at the top
  # of this file.
  sess$sources <- list(source)
  sess <- ontologyDiscoverR::pop_extract(sess, verbose = FALSE)
  list(entities      = sess$instances$entities,
       relationships = sess$instances$relationships,
       amendments    = sess$amendments)
}

#' Normalise engine output into the shape the store expects
#' @keywords internal
normalise_population <- function(raw) {
  entities <- lapply(raw$entities %||% list(), function(e) {
    ref <- if (length(e$source_refs %||% list()) > 0) e$source_refs[[1]] else list()
    list(
      instance_id  = e$instance_id %||% orph_id("inst"),
      type_id      = e$type_id %||% "Unknown",
      properties   = e$properties %||% list(),
      confidence   = orph_snap_confidence(e$confidence %||% 0.8),
      excerpt      = ref$excerpt %||% e$excerpt %||% "",
      source_label = ref$source_label %||% "",
      page_no      = e$page_no %||% page_from_excerpt(ref$excerpt %||% e$excerpt %||% "")
    )
  })

  relationships <- lapply(raw$relationships %||% list(), function(r) {
    list(
      link_type_id     = r$link_type_id %||% "unknown",
      from_instance_id = r$from_instance_id %||% "",
      to_instance_id   = r$to_instance_id %||% "",
      evidence         = r$evidence %||% "",
      confidence       = orph_snap_confidence(r$confidence %||% 0.8)
    )
  })

  amendments <- lapply(raw$amendments %||% list(), function(a) {
    list(
      amendment_type = a$amendment_type %||% "new_property",
      type_id        = a$type_id %||% NA_character_,
      property_id    = a$property_id %||% NA_character_,
      inferred_type  = a$inferred_type %||% "string",
      observed_value = a$evidence %||% a$observed_value %||% "",
      rationale      = a$rationale %||% ""
    )
  })

  # An engine may report a relationship against an instance it did not return.
  # Dropping those here keeps the edge table referentially sound instead of
  # deferring the problem to whoever queries it.
  known <- vapply(entities, function(e) e$instance_id, character(1))
  ok <- vapply(relationships, function(r)
    r$from_instance_id %in% known && r$to_instance_id %in% known, logical(1))
  dropped <- sum(!ok)
  list(entities = entities, relationships = relationships[ok],
       amendments = amendments, dropped_edges = dropped)
}

#' Recover a page number from a page-marked excerpt
#' @keywords internal
page_from_excerpt <- function(excerpt) {
  m <- regmatches(excerpt, regexpr("--- Page ([0-9]+) ---", excerpt))
  if (length(m) == 0 || !nzchar(m[[1]])) return(NA_integer_)
  as.integer(gsub("\\D", "", m[[1]]))
}
