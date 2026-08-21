#' @keywords internal
`%||%` <- function(x, y) if (is.null(x)) y else x

# Package-level mutable state: registered providers, engines, and transaction
# depth. Kept in one environment so there is a single place to look for
# anything that is not a pure function of its arguments.
orph_env <- new.env(parent = emptyenv())

# ---------------------------------------------------------------------------
# Identifiers and time
# ---------------------------------------------------------------------------

#' Generate an identifier
#'
#' Prefixed random hex. Prefixes make ids self-describing in the audit trail,
#' which matters when a row id turns up in `edit_history` detached from its
#' table.
#'
#' @param prefix Short string prepended to the id.
#' @return A character scalar.
#' @keywords internal
orph_id <- function(prefix = "obj") {
  paste0(prefix, "_", paste0(
    format(as.hexmode(sample.int(65536L, 8L, replace = TRUE) - 1L), width = 4L),
    collapse = ""
  ))
}

#' Current UTC timestamp in ISO-8601
#' @keywords internal
orph_now <- function() format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")

# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

#' Serialise to JSON for storage in a TEXT column
#' @keywords internal
to_json <- function(x) {
  if (is.null(x)) return(NA_character_)
  jsonlite::toJSON(x, auto_unbox = TRUE, null = "null", na = "null")
}

#' Deserialise a JSON TEXT column
#' @keywords internal
from_json <- function(x) {
  if (is.null(x) || length(x) == 0 || is.na(x) || !nzchar(x)) return(NULL)
  jsonlite::fromJSON(x, simplifyVector = FALSE)
}

# ---------------------------------------------------------------------------
# Confidence rubric
# ---------------------------------------------------------------------------

#' The confidence rubric
#'
#' The platform deliberately does not store arbitrary floats. Every confidence
#' is one of five rubric levels, so that "0.7" always means the same thing to
#' every reviewer rather than being an opaque model score.
#'
#' @format A named numeric vector.
#' @export
ORPH_CONFIDENCE <- c(
  explicit    = 1.0,  # explicit in the schema / stated verbatim in the document
  named       = 0.9,  # clearly named with its attributes listed
  implied     = 0.7,  # mentioned as a concept with structure implied
  inferred    = 0.5,  # inferred from surrounding context
  speculative = 0.2   # speculative
)

#' Snap an arbitrary score onto the confidence rubric
#'
#' Extraction backends return arbitrary floats. Storing those directly would
#' quietly abandon the rubric, so every score is snapped to the nearest rubric
#' level at the persistence boundary. Snapping is downward-biased: a score is
#' only promoted to a higher level when it is at least as high as that level,
#' so the pipeline never reports more certainty than the backend claimed.
#'
#' @param score Numeric vector of raw scores.
#' @return Numeric vector of rubric levels.
#' @export
orph_snap_confidence <- function(score) {
  levels <- sort(unname(ORPH_CONFIDENCE))
  vapply(score, function(s) {
    if (is.null(s) || is.na(s)) return(unname(ORPH_CONFIDENCE[["inferred"]]))
    s <- max(0, min(1, as.numeric(s)))
    eligible <- levels[levels <= s + 1e-9]
    if (length(eligible) == 0) min(levels) else max(eligible)
  }, numeric(1), USE.NAMES = FALSE)
}

#' Human-readable label for a rubric level
#' @param value Numeric rubric level.
#' @return Character label, or `"unknown"`.
#' @export
orph_confidence_label <- function(value) {
  vapply(value, function(v) {
    hit <- names(ORPH_CONFIDENCE)[abs(ORPH_CONFIDENCE - v) < 1e-9]
    if (length(hit) == 0) "unknown" else hit[[1]]
  }, character(1), USE.NAMES = FALSE)
}

# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

#' Provenance source values
#' @export
ORPH_SOURCES <- c("ai_local", "ai_cloud", "human")

#' Instance review status values
#' @export
ORPH_STATUSES <- c("unconfirmed", "confirmed", "amended", "rejected")

#' Statuses whose rows are excluded from downstream use
#' @keywords internal
ORPH_EXCLUDED_STATUSES <- c("rejected")

#' @keywords internal
assert_choice <- function(value, choices, arg) {
  if (length(value) != 1L || !value %in% choices) {
    cli::cli_abort("{.arg {arg}} must be one of {.val {choices}}, not {.val {value}}.")
  }
  invisible(value)
}

#' @keywords internal
assert_string <- function(value, arg) {
  if (!is.character(value) || length(value) != 1L || is.na(value) || !nzchar(value)) {
    cli::cli_abort("{.arg {arg}} must be a non-empty string.")
  }
  invisible(value)
}

#' Normalise a name for naive cross-document matching
#'
#' Deliberately crude: lowercase, strip punctuation, drop common company
#' suffixes and collapse whitespace. This is the stepping stone described in
#' the roadmap, not entity resolution -- anything relying on it must be
#' labelled unresolved. See [orph_corpus_analysis()].
#'
#' @param x Character vector of raw names.
#' @return Character vector of normalised keys.
#' @export
orph_naive_key <- function(x) {
  x <- tolower(as.character(x %||% ""))
  x <- gsub("[[:punct:]]", " ", x)
  x <- gsub("\\b(limited|ltd|plc|llp|llc|inc|incorporated|company|co|group|holdings|the)\\b",
            " ", x)
  x <- gsub("\\s+", " ", x)
  trimws(x)
}
