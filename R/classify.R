# ---------------------------------------------------------------------------
# Pipeline step 3: classification.
#
# Always the local tier. Classification reads the whole document, so routing it
# to the cloud would mean sending every ingested document off-site to learn
# what it is -- the opposite of an opt-in.
# ---------------------------------------------------------------------------

#' Document types the classifier chooses between
#' @export
ORPH_DOC_TYPES <- c("contract", "amendment", "tender", "correspondence", "other")

#' @keywords internal
classify_system_prompt <- function(doc_types = ORPH_DOC_TYPES) {
  paste0(
    "You classify documents held by a public-sector contract analysis team.\n",
    "Return JSON with keys:\n",
    '  doc_type      one of: ', paste(doc_types, collapse = ", "), "\n",
    '  sector        the public sector domain (e.g. health, transport, education, ict), or null\n',
    '  jurisdiction  the governing jurisdiction if stated or clearly inferable, or null\n',
    '  confidence    one of 1.0, 0.9, 0.7, 0.5, 0.2\n',
    '  rationale     one short sentence\n\n',
    "Use the confidence rubric strictly:\n",
    "  1.0 stated explicitly in the document\n",
    "  0.9 clearly identifiable from headings and structure\n",
    "  0.7 implied by the content\n",
    "  0.5 inferred from context\n",
    "  0.2 speculative\n",
    "Return null rather than guessing a sector or jurisdiction that is not supported by the text."
  )
}

#' Classify a document
#'
#' Writes `doc_type`, `sector` and `jurisdiction` onto the document row with
#' provenance, leaving it `unconfirmed` until a human confirms or amends it,
#' the same as any other AI-sourced value.
#'
#' @param con A writable connection.
#' @param document_id Document identifier.
#' @param actor_id Actor triggering the classification.
#' @param max_chars How much of the document to send to the local model.
#' @return A list with the classification and its confidence.
#' @export
orph_classify <- function(con, document_id, actor_id = NULL, max_chars = 12000L) {
  assert_writable(con)
  doc <- orph_get_document(con, document_id)
  if (is.null(doc)) cli::cli_abort("No document {.val {document_id}}.")

  if (!orph_document_has_text(con, document_id)) {
    cli::cli_abort(c(
      "Document {.val {document_id}} has no extracted text to classify.",
      i = "Its pages may need OCR -- check {.field text_source} on document_pages."
    ))
  }
  text <- orph_document_text(con, document_id)
  if (nchar(text) > max_chars) text <- substr(text, 1, max_chars)

  doc_types <- orph_document_types(orph_active_bundle(con))
  reply <- orph_llm_json(con, "local", classify_system_prompt(doc_types), text,
                         purpose = "classify", document_id = document_id,
                         actor_id = actor_id, excerpt_only = nchar(text) >= max_chars)

  doc_type <- reply$doc_type %||% "other"
  if (!(doc_type %in% doc_types)) doc_type <- "other"
  confidence <- orph_snap_confidence(reply$confidence %||% 0.5)

  previous <- list(doc_type = doc$doc_type, sector = doc$sector, jurisdiction = doc$jurisdiction)
  new <- list(doc_type = doc_type,
              sector = as_scalar_or_na(reply$sector),
              jurisdiction = as_scalar_or_na(reply$jurisdiction))

  with_tx(con, {
    DBI::dbExecute(con,
      "UPDATE documents SET doc_type = ?, sector = ?, jurisdiction = ?,
         classification_source = 'ai_local', classification_confidence = ?,
         classification_status = 'unconfirmed'
       WHERE document_id = ?",
      params = list(new$doc_type, new$sector, new$jurisdiction, confidence, document_id))
    record_edit(con, "documents", document_id, document_id, "classify",
                previous = previous, new = c(new, list(confidence = confidence)),
                actor_id = actor_id, note = reply$rationale %||% NULL)
  })

  list(document_id = document_id, doc_type = new$doc_type, sector = new$sector,
       jurisdiction = new$jurisdiction, confidence = confidence,
       confidence_label = orph_confidence_label(confidence),
       status = "unconfirmed", rationale = reply$rationale %||% NA_character_)
}

#' @keywords internal
as_scalar_or_na <- function(x) {
  if (is.null(x) || length(x) == 0) return(NA_character_)
  x <- as.character(x)[[1]]
  if (is.na(x) || !nzchar(x) || tolower(x) %in% c("null", "unknown", "n/a")) NA_character_ else x
}
