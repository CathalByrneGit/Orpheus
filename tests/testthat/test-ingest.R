test_that("a text document is ingested with pages, hash and audit row", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  path <- write_contract_file()
  res <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)

  expect_false(res$duplicate)
  expect_equal(res$n_pages, 1)
  expect_equal(res$text_source, "native")

  doc <- orph_get_document(con, res$document_id)
  expect_equal(nchar(doc$file_hash), 64)
  expect_true(file.exists(doc$storage_path))
  expect_equal(doc$review_status, "unreviewed")
  expect_equal(doc$visibility, "private")
  expect_equal(nrow(orph_document_history(con, res$document_id)), 1)
})

test_that("dedup is on content, not filename", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  path <- write_contract_file()
  first <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)

  copy <- file.path(tempdir(), "a-different-name.txt")
  file.copy(path, copy, overwrite = TRUE)
  second <- orph_ingest(con, copy, actor_id = "act_test", storage_root = root)

  expect_true(second$duplicate)
  expect_equal(second$document_id, first$document_id)
  expect_equal(nrow(orph_list_documents(con)), 1)
})

test_that("form feeds split a document into pages and text is retrievable by page", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  path <- file.path(tempdir(), paste0(as.integer(runif(1, 1, 1e9)), "-multi.txt"))
  writeLines(c("PAGE ONE CONTENT that is long enough to count as real text.", "\f",
               "PAGE TWO CONTENT that is also long enough to count as text."), path)
  res <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)

  expect_equal(res$n_pages, 2)
  text <- orph_document_text(con, res$document_id)
  expect_match(text, "--- Page 1 ---")
  expect_match(text, "--- Page 2 ---")
  expect_match(orph_document_text(con, res$document_id, page_markers = FALSE), "PAGE TWO")
})

test_that("a page with no usable text is recorded as needing OCR, not as empty", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  old <- orph_set_ocr_provider(NULL); on.exit(orph_set_ocr_provider(old))

  path <- file.path(tempdir(), paste0(as.integer(runif(1, 1, 1e9)), "-blank.txt"))
  writeLines("", path)
  res <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)

  expect_equal(res$needs_ocr, 1)
  expect_equal(res$text_source, "needs_ocr")
  pages <- DBI::dbGetQuery(con, "SELECT text_source FROM document_pages WHERE document_id = ?",
                           params = list(res$document_id))
  expect_equal(pages$text_source, "needs_ocr")
})

test_that("a registered OCR provider is used and reported", {
  old <- orph_set_ocr_provider(function(image_path) "TEXT RECOVERED BY OCR")
  on.exit(orph_set_ocr_provider(old))
  caps <- orph_extraction_capabilities()
  expect_true(caps$ocr)
  expect_equal(caps$ocr_backend, "custom")
})

test_that("unsupported file types are refused with a usable message", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  path <- write_contract_file()
  expect_error(orph_ingest(con, path, filename = "old.doc", storage_root = root),
               "Legacy")
  expect_error(orph_ingest(con, path, filename = "thing.xyz", storage_root = root),
               "unrecognised file type")
  expect_error(orph_ingest(con, "/no/such/file.pdf", storage_root = root), "No file at")
})

test_that("docx text is extracted without an external tool", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  dir <- file.path(tempdir(), paste0("docx-", as.integer(runif(1, 1, 1e9))))
  dir.create(file.path(dir, "word"), recursive = TRUE)
  writeLines(paste0(
    '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/',
    'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>FRAMEWORK AGREEMENT</w:t>',
    '</w:r></w:p><w:p><w:r><w:t>Cork City Council &amp; Byrne Ltd.</w:t></w:r>',
    '</w:p></w:body></w:document>'), file.path(dir, "word", "document.xml"))
  docx <- file.path(tempdir(), paste0(as.integer(runif(1, 1, 1e9)), "-test.docx"))
  withr::with_dir(dir, utils::zip(docx, list.files(".", recursive = TRUE), flags = "-qr"))
  skip_if_not(file.exists(docx), "zip unavailable")

  res <- orph_ingest(con, docx, actor_id = "act_test", storage_root = root)
  text <- orph_document_text(con, res$document_id)
  expect_match(text, "FRAMEWORK AGREEMENT")
  expect_match(text, "Cork City Council & Byrne Ltd.", fixed = TRUE)
})
