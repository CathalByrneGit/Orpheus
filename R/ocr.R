# ---------------------------------------------------------------------------
# Text extraction and OCR.
#
# The OCR tool is an open decision (roadmap Phase 1.5). Committing to one here
# would be guessing, so this is a provider registry: extraction asks for the
# configured provider and gets whatever the deployment has installed. Swapping
# tesseract for a cloud OCR service, or for whatever evaluation settles on, is
# a call to orph_set_ocr_provider() rather than an edit to the pipeline.
# ---------------------------------------------------------------------------

#' Register the OCR provider
#'
#' @param fn A function `f(image_path)` returning the page's text as a single
#'   string, or `NULL` to unset. The pipeline calls it once per page image.
#' @return Invisibly, the previous provider.
#' @export
orph_set_ocr_provider <- function(fn) {
  previous <- orph_env$ocr_provider
  if (!is.null(fn) && !is.function(fn)) cli::cli_abort("{.arg fn} must be a function or NULL.")
  orph_env$ocr_provider <- fn
  invisible(previous)
}

#' The active OCR provider
#'
#' Falls back to whichever backend is installed: the tesseract R package, then
#' a `tesseract` binary on PATH. Returns `NULL` when neither is present, in
#' which case image-only pages are recorded as needing OCR rather than being
#' silently dropped.
#'
#' @return A function or `NULL`.
#' @export
orph_ocr_provider <- function() {
  if (!is.null(orph_env$ocr_provider)) return(orph_env$ocr_provider)

  if (requireNamespace("tesseract", quietly = TRUE)) {
    return(function(image_path) paste(tesseract::ocr(image_path), collapse = "\n"))
  }
  if (nzchar(Sys.which("tesseract"))) {
    return(function(image_path) {
      out <- tempfile()
      status <- suppressWarnings(system2("tesseract", c(shQuote(image_path), shQuote(out)),
                                         stdout = FALSE, stderr = FALSE))
      txt <- paste0(out, ".txt")
      if (status != 0 || !file.exists(txt)) return("")
      on.exit(unlink(txt), add = TRUE)
      paste(readLines(txt, warn = FALSE), collapse = "\n")
    })
  }
  NULL
}

#' Describe the text-extraction backends available in this deployment
#'
#' Exposed over the API so an operator can see what the running server can
#' actually read before a user uploads something it cannot.
#'
#' @return A list of backend availability flags.
#' @export
orph_extraction_capabilities <- function() {
  list(
    pdf_text     = requireNamespace("pdftools", quietly = TRUE) || nzchar(Sys.which("pdftotext")),
    pdf_render   = requireNamespace("pdftools", quietly = TRUE) || nzchar(Sys.which("pdftoppm")),
    docx         = TRUE,
    plain_text   = TRUE,
    ocr          = !is.null(orph_ocr_provider()),
    ocr_backend  = if (requireNamespace("tesseract", quietly = TRUE)) "tesseract-r"
                   else if (nzchar(Sys.which("tesseract"))) "tesseract-cli"
                   else if (!is.null(orph_env$ocr_provider)) "custom"
                   else NA_character_
  )
}

# ---------------------------------------------------------------------------
# Per-format text extraction
# ---------------------------------------------------------------------------

#' A page below this many characters is treated as image-only and sent to OCR
#' @keywords internal
OCR_CHAR_THRESHOLD <- 40L

#' @keywords internal
extract_pdf_pages <- function(path) {
  if (requireNamespace("pdftools", quietly = TRUE)) {
    return(as.character(pdftools::pdf_text(path)))
  }
  if (nzchar(Sys.which("pdftotext"))) {
    out <- tempfile(fileext = ".txt")
    on.exit(unlink(out), add = TRUE)
    status <- suppressWarnings(system2("pdftotext", c("-layout", shQuote(path), shQuote(out)),
                                       stdout = FALSE, stderr = FALSE))
    if (status != 0 || !file.exists(out)) {
      cli::cli_abort("pdftotext failed on {.path {basename(path)}}.")
    }
    text <- paste(readLines(out, warn = FALSE), collapse = "\n")
    # pdftotext separates pages with a form feed.
    return(strsplit(text, "\f", fixed = TRUE)[[1]])
  }
  cli::cli_abort(c(
    "No PDF text backend is available.",
    i = "Install the {.pkg pdftools} R package, or put {.code pdftotext} on PATH."
  ))
}

#' @keywords internal
render_pdf_pages <- function(path, out_dir, pages) {
  dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
  if (requireNamespace("pdftools", quietly = TRUE)) {
    return(tryCatch(
      pdftools::pdf_convert(path, format = "png", pages = pages, dpi = 150,
                            filenames = file.path(out_dir, sprintf("page-%03d.png", pages)),
                            verbose = FALSE),
      error = function(e) character()))
  }
  if (nzchar(Sys.which("pdftoppm"))) {
    return(unlist(lapply(pages, function(i) {
      stem <- file.path(out_dir, sprintf("page-%03d", i))
      status <- suppressWarnings(system2("pdftoppm",
        c("-png", "-r", "150", "-f", i, "-l", i, shQuote(path), shQuote(stem)),
        stdout = FALSE, stderr = FALSE))
      hits <- list.files(out_dir, pattern = sprintf("^page-%03d", i), full.names = TRUE)
      if (status == 0 && length(hits)) hits[[1]] else NULL
    })))
  }
  character()
}

#' @keywords internal
extract_docx_text <- function(path) {
  # Pure-R docx reading: a .docx is a zip whose word/document.xml holds the
  # body. Avoids a dependency for what is a small amount of string handling.
  tmp <- tempfile(); dir.create(tmp)
  on.exit(unlink(tmp, recursive = TRUE), add = TRUE)
  files <- tryCatch(utils::unzip(path, exdir = tmp), error = function(e) character())
  doc <- file.path(tmp, "word", "document.xml")
  if (!file.exists(doc)) cli::cli_abort("{.path {basename(path)}} is not a readable .docx.")

  xml <- paste(readLines(doc, warn = FALSE, encoding = "UTF-8"), collapse = "")
  xml <- gsub("<w:tab[^>]*/>", "\t", xml)
  xml <- gsub("<w:br[^>]*/>", "\n", xml)
  xml <- gsub("</w:p>", "\n", xml, fixed = TRUE)
  # Explicit page breaks are the only page signal a docx gives.
  xml <- gsub('<w:lastRenderedPageBreak[^>]*/>', "\f", xml)
  text <- gsub("<[^>]+>", "", xml)
  text <- gsub("&lt;", "<", text, fixed = TRUE)
  text <- gsub("&gt;", ">", text, fixed = TRUE)
  text <- gsub("&amp;", "&", text, fixed = TRUE)
  text <- gsub("&quot;", '"', text, fixed = TRUE)
  text <- gsub("&apos;", "'", text, fixed = TRUE)
  text <- gsub("[ \t]+\n", "\n", text)
  text <- gsub("\n{3,}", "\n\n", text)
  trimws(text)
}

#' @keywords internal
detect_kind <- function(path) {
  ext <- tolower(tools::file_ext(path))
  switch(ext,
    pdf  = "pdf",
    docx = "docx",
    doc  = "unsupported_doc",
    txt  = "text", md = "text", markdown = "text",
    png  = "image", jpg = "image", jpeg = "image", tif = "image", tiff = "image",
    bmp  = "image",
    "unknown")
}

#' @keywords internal
mime_for <- function(kind, path) {
  switch(kind,
    pdf   = "application/pdf",
    docx  = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    text  = "text/plain",
    image = paste0("image/", tolower(tools::file_ext(path))),
    "application/octet-stream")
}
