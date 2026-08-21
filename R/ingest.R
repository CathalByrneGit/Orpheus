# ---------------------------------------------------------------------------
# Pipeline step 1: ingest.
#
# Raw file in, text and page records out, with the original preserved
# content-addressed so an extraction can always be re-run against exactly the
# bytes it was derived from.
# ---------------------------------------------------------------------------

#' Where uploaded originals are kept
#'
#' Content-addressed by SHA-256: the same file ingested twice occupies one
#' copy, and a stored path is self-verifying.
#'
#' @param root Storage root directory.
#' @param hash File hash.
#' @param ext File extension.
#' @keywords internal
storage_path_for <- function(root, hash, ext) {
  sub_dir <- file.path(root, "documents", substr(hash, 1, 2))
  dir.create(sub_dir, recursive = TRUE, showWarnings = FALSE)
  file.path(sub_dir, paste0(hash, if (nzchar(ext)) paste0(".", ext) else ""))
}

#' Split extracted text into page records
#' @keywords internal
build_pages <- function(page_texts, source_label) {
  lapply(seq_along(page_texts), function(i) {
    txt <- page_texts[[i]] %||% ""
    list(page_no = i, text = txt, char_count = nchar(txt),
         text_source = if (nchar(trimws(txt)) >= OCR_CHAR_THRESHOLD) "native" else "pending_ocr")
  })
}

#' Ingest a document
#'
#' Extracts text, records one row per page, hashes the file for dedup, and
#' copies the original into the content-addressed store. Pages that yield too
#' little text to be real text are rendered to images and sent to the
#' configured OCR provider; with no provider configured they are recorded as
#' `needs_ocr` rather than being passed off as empty pages.
#'
#' @param con A writable connection.
#' @param path Path to the file to ingest.
#' @param actor_id Actor performing the ingest.
#' @param storage_root Directory for originals and page images.
#' @param filename Original filename, if different from `basename(path)`.
#' @param visibility `"private"`, `"link-view"` or `"link-edit"`.
#' @return A list with the document id, page count, and whether this was a
#'   duplicate of an existing document.
#' @export
orph_ingest <- function(con, path, actor_id = NULL, storage_root = "storage",
                        filename = NULL, visibility = c("private", "link-view", "link-edit")) {
  assert_writable(con)
  visibility <- match.arg(visibility)
  if (!file.exists(path)) cli::cli_abort("No file at {.path {path}}.")

  filename <- filename %||% basename(path)
  kind     <- detect_kind(filename)
  if (kind == "unsupported_doc") {
    cli::cli_abort(c("Legacy {.field .doc} files are not supported.",
                     i = "Convert to {.field .docx} or PDF before ingesting."))
  }
  if (kind == "unknown") {
    cli::cli_abort("Cannot ingest {.path {filename}}: unrecognised file type.")
  }

  hash <- digest::digest(file = path, algo = "sha256")

  # Dedup is on content, not filename: the same contract mailed round twice
  # under different names is one document.
  existing <- db_get_one(con, "SELECT document_id, filename FROM documents WHERE file_hash = ?",
                         list(hash))
  if (!is.null(existing)) {
    return(list(document_id = existing$document_id, duplicate = TRUE,
                filename = existing$filename, n_pages = NA_integer_,
                message = sprintf("Identical content already ingested as '%s'.", existing$filename)))
  }

  stored <- storage_path_for(storage_root, hash, tolower(tools::file_ext(filename)))
  file.copy(path, stored, overwrite = TRUE)

  page_texts <- switch(kind,
    pdf   = extract_pdf_pages(stored),
    docx  = strsplit(extract_docx_text(stored), "\f", fixed = TRUE)[[1]],
    text  = strsplit(paste(readLines(stored, warn = FALSE), collapse = "\n"), "\f", fixed = TRUE)[[1]],
    image = "",
    cli::cli_abort("Unhandled document kind: {kind}"))
  if (length(page_texts) == 0) page_texts <- ""

  pages <- build_pages(page_texts, filename)
  document_id <- orph_id("doc")

  # OCR fallback for pages that produced no usable text.
  needs_ocr <- which(vapply(pages, function(p) identical(p$text_source, "pending_ocr"), logical(1)))
  image_dir <- file.path(storage_root, "pages", document_id)
  images    <- character()
  if (length(needs_ocr) > 0) {
    images <- if (kind == "pdf") render_pdf_pages(stored, image_dir, needs_ocr)
              else if (kind == "image") stored else character()
    ocr <- orph_ocr_provider()
    for (idx in seq_along(needs_ocr)) {
      i   <- needs_ocr[[idx]]
      img <- if (length(images) >= idx) images[[idx]] else NA_character_
      pages[[i]]$image_path <- if (is.na(img)) NULL else img
      if (!is.null(ocr) && !is.na(img) && file.exists(img)) {
        text <- tryCatch(ocr(img), error = function(e) "")
        pages[[i]]$text        <- text
        pages[[i]]$char_count  <- nchar(text)
        pages[[i]]$text_source <- if (nzchar(trimws(text))) "ocr" else "needs_ocr"
      } else {
        # No OCR backend, or nothing to render. The page is recorded as
        # unreadable so review can see the gap instead of inferring one from
        # an empty page.
        pages[[i]]$text_source <- "needs_ocr"
      }
    }
  }

  sources <- unique(vapply(pages, function(p) p$text_source, character(1)))
  doc_text_source <- if (length(sources) == 1) sources else "mixed"

  with_tx(con, {
    db_insert(con, "documents", list(
      document_id  = document_id,
      filename     = filename,
      file_hash    = hash,
      mime_type    = mime_for(kind, filename),
      byte_size    = as.integer(file.info(stored)$size),
      storage_path = stored,
      n_pages      = length(pages),
      text_source  = doc_text_source,
      date_added   = orph_now(),
      created_by   = actor_id,
      visibility   = visibility,
      review_status = "unreviewed"
    ))
    for (p in pages) {
      db_insert(con, "document_pages", list(
        document_id = document_id,
        page_no     = p$page_no,
        text        = p$text,
        text_source = p$text_source,
        image_path  = p$image_path %||% NA_character_,
        char_count  = p$char_count
      ))
    }
    record_edit(con, "documents", document_id, document_id, "ingest",
                previous = NULL, new = list(filename = filename, file_hash = hash),
                actor_id = actor_id)
  })

  list(document_id = document_id, duplicate = FALSE, filename = filename,
       n_pages = length(pages),
       needs_ocr = sum(vapply(pages, function(p) identical(p$text_source, "needs_ocr"), logical(1))),
       text_source = doc_text_source)
}

#' Full extracted text of a document
#'
#' @param con A connection.
#' @param document_id Document identifier.
#' @param page_markers Prefix each page with a `--- Page n ---` marker, which
#'   is what lets a model cite a page number in an excerpt.
#' @return A single string.
#' @export
orph_document_text <- function(con, document_id, page_markers = TRUE) {
  pages <- db_query(con,
    "SELECT page_no, text FROM document_pages WHERE document_id = ? ORDER BY page_no",
    list(document_id))
  if (nrow(pages) == 0) return("")
  if (!page_markers) return(paste(pages$text, collapse = "\n"))
  paste(sprintf("--- Page %d ---\n%s", pages$page_no, pages$text %||% ""), collapse = "\n\n")
}

#' Does a document have any usable text?
#'
#' Checked against the page rows rather than against orph_document_text(),
#' whose `--- Page n ---` markers make a document with no content look
#' non-empty. Without this, a scan that failed OCR would go to the model as a
#' prompt containing nothing but page headers.
#'
#' @param con A connection.
#' @param document_id Document identifier.
#' @return `TRUE` if any page has non-whitespace text.
#' @export
orph_document_has_text <- function(con, document_id) {
  pages <- db_query(con, "SELECT text FROM document_pages WHERE document_id = ?",
                    list(document_id))
  if (nrow(pages) == 0) return(FALSE)
  any(nzchar(trimws(pages$text %||% "")))
}

#' Document metadata
#' @param con A connection.
#' @param document_id Document identifier.
#' @return A one-row list, or `NULL`.
#' @export
orph_get_document <- function(con, document_id) {
  db_get_one(con, "SELECT * FROM documents WHERE document_id = ?", list(document_id))
}

#' List documents
#' @param con A connection.
#' @param limit Maximum rows.
#' @return A data frame.
#' @export
orph_list_documents <- function(con, limit = 100L) {
  db_query(con, "SELECT document_id, filename, doc_type, sector, jurisdiction,
                        review_status, visibility, n_pages, date_added, created_by
                 FROM documents ORDER BY date_added DESC LIMIT ?", list(as.integer(limit)))
}
