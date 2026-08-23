# Everything else in the suite feeds ingest a .txt file, which skips the whole
# PDF path -- and "drop a contract PDF into the app" is the first line of what
# Phase 1 is for. These tests run a real two-page PDF through it.
#
# The fixture is built by data-raw/make_test_pdf.py: a small uncompressed PDF
# with a genuine text layer, so pdftotext has to find both the text and the
# page break rather than being handed them.

skip_if_no_pdf_text <- function() {
  caps <- orph_extraction_capabilities()
  testthat::skip_if_not(isTRUE(caps$pdf_text),
                        "needs pdftools or pdftotext on PATH")
}

fixture_pdf <- function() {
  testthat::test_path("fixtures", "services-agreement.pdf")
}

test_that("a real PDF ingests with its text layer and page breaks intact", {
  skip_if_no_pdf_text()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)

  ing <- orph_ingest(con, fixture_pdf(), actor_id = "act_test", storage_root = root)
  doc <- orph_get_document(con, ing$document_id)

  expect_equal(doc$mime_type, "application/pdf")
  expect_equal(doc$n_pages, 2)

  # Not OCR: the fixture has a text layer, so the scanned-document path must
  # not have been taken. A silent fall through to OCR on a text PDF would be
  # slow and lossy in a way nothing else here would notice.
  expect_equal(doc$text_source, "native")

  pages <- DBI::dbGetQuery(con,
    "SELECT page_no, text FROM document_pages ORDER BY page_no")
  expect_equal(nrow(pages), 2)

  # Split in the right place, not merely split into two.
  expect_match(pages$text[1], "SERVICES AGREEMENT", fixed = TRUE)
  expect_match(pages$text[1], "1,480,000", fixed = TRUE)
  expect_match(pages$text[2], "Limitation of liability", fixed = TRUE)
  expect_false(grepl("Limitation of liability", pages$text[1], fixed = TRUE))
})

test_that("the deterministic pass finds the dates and money in a real PDF", {
  skip_if_no_pdf_text()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = function(...)
    list(entities = list(), relationships = list(), amendments = list()))

  ing <- orph_ingest(con, fixture_pdf(), actor_id = "act_test", storage_root = root)
  orph_extract(con, ing$document_id, tier = "local", actor_id = "act_test")

  dates <- DBI::dbGetQuery(con,
    "SELECT value, date_role, page_no FROM instances_KeyDate ORDER BY value")
  expect_setequal(dates$value,
                  c("2025-11-12", "2026-02-04", "2026-03-01", "2029-02-28"))

  # Every finding carries the page it was read from, and all of these are on
  # page 1. Page attribution is the part of provenance a multi-page document
  # can get wrong without anyone noticing.
  expect_true(all(dates$page_no == 1))

  amounts <- DBI::dbGetQuery(con,
    "SELECT amount, currency, role, page_no FROM instances_MonetaryAmount")
  expect_equal(nrow(amounts), 1)
  expect_equal(amounts$amount, 1480000)
  expect_equal(amounts$currency, "EUR")
  expect_equal(amounts$role, "contract_value")
  expect_equal(amounts$page_no, 1)
})

test_that("'terminates on' is read as an end date, not a start date", {
  # The regression that made the fixture worth having. DATE_ROLE_CUES carried
  # "terminate on", which does not match "terminates on" -- the ordinary way a
  # contract says it. With no end cue matching, the nearest surviving cue was
  # "commences" several clauses earlier, so the termination date was stored as
  # the start date: not a missing fact but a confidently wrong one.
  roles <- function(value, dates) dates$date_role[dates$value == value]

  skip_if_no_pdf_text()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = function(...)
    list(entities = list(), relationships = list(), amendments = list()))

  ing <- orph_ingest(con, fixture_pdf(), actor_id = "act_test", storage_root = root)
  orph_extract(con, ing$document_id, tier = "local", actor_id = "act_test")
  dates <- DBI::dbGetQuery(con, "SELECT value, date_role FROM instances_KeyDate")

  expect_equal(roles("2026-03-01", dates), "start")   # "commences on"
  expect_equal(roles("2029-02-28", dates), "end")     # "terminates on"
})

test_that("date cues are stems, so ordinary inflection still matches", {
  # Written as a property of the table rather than of one document: an exact
  # phrase added here would pass every test above and still miss real text.
  inflected <- list(
    end   = c("This Agreement terminates on 3 April 2027.",
              "The licence expires on 3 April 2027.",
              "The Agreement terminating on 3 April 2027 shall not renew."),
    start = c("The term commences on 3 April 2027.",
              "Work commencing on 3 April 2027 is in scope.")
  )
  for (role in names(inflected)) {
    for (text in inflected[[role]]) {
      pos <- regexpr("3 April 2027", text, fixed = TRUE)
      expect_equal(infer_role(text, pos, DATE_ROLE_CUES), role, info = text)
    }
  }
})
