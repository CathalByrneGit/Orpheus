test_that("dates are found in the forms contracts actually use", {
  found <- orph_find_dates(paste(
    "Commencing on 1 January 2024, expiring 2026-12-31,",
    "signed December 3, 2025, and dated 03/04/2025."))
  expect_setequal(found$value, c("2024-01-01", "2026-12-31", "2025-12-03", "2025-04-03"))
})

test_that("an ambiguous day/month order is recorded at a lower rubric level", {
  # 03/04/2025 could be 3 April or 4 March. Day-first is assumed, and the
  # assumption is priced into the confidence rather than hidden.
  ambiguous <- orph_find_dates("dated 03/04/2025")
  expect_true(ambiguous$ambiguous)
  expect_equal(ambiguous$confidence, 0.5)

  # 31/12/2025 cannot be month-first, so there is nothing to be unsure about.
  unambiguous <- orph_find_dates("dated 31/12/2025")
  expect_false(unambiguous$ambiguous)
  expect_equal(unambiguous$confidence, 1.0)
  expect_equal(unambiguous$value, "2025-12-31")
})

test_that("monetary amounts are found with their currency", {
  found <- orph_find_amounts("Total value EUR 2,400,000, capped at 500,000 euro, plus $1,250 per day.")
  expect_setequal(found$amount, c(2400000, 500000, 1250))
  expect_equal(found$currency[found$amount == 2400000], "EUR")
  expect_equal(found$currency[found$amount == 1250], "USD")
})

test_that("a currency read from a word scores below one read from a code", {
  code <- orph_find_amounts("EUR 500,000")
  word <- orph_find_amounts("500,000 euro")
  expect_equal(code$confidence, 1.0)
  expect_equal(word$confidence, 0.9)
})

test_that("no dates or amounts yields an empty frame, not an error", {
  expect_equal(nrow(orph_find_dates("nothing of interest here")), 0)
  expect_equal(nrow(orph_find_amounts("nothing of interest here")), 0)
  expect_equal(nrow(orph_find_dates("")), 0)
})

test_that("the role of a date comes from the nearest cue, not the first one listed", {
  infer_role <- orpheus:::infer_role
  cues <- orpheus:::DATE_ROLE_CUES
  text <- "Commencing on 1 January 2024 and shall expire on 2026-12-31."
  start_pos <- regexpr("1 January 2024", text, fixed = TRUE)
  end_pos   <- regexpr("2026-12-31", text, fixed = TRUE)
  expect_equal(infer_role(text, start_pos, cues), "start")
  # Both cues are within the window; the expiry date must not inherit
  # "commencing" simply because start is checked first.
  expect_equal(infer_role(text, end_pos, cues), "end")
})

test_that("currency symbols are matched regardless of the server's locale", {
  # A euro sign written as a \\u escape is marked UTF-8 by R, while document
  # text read off disk in the C locale is not. Matching one against the other
  # fails and returns no euro amounts at all -- which on an Irish contract looks
  # exactly like a document with no money in it. The symbols are therefore built
  # from raw bytes and matched with useBytes = TRUE.
  euro  <- orpheus:::utf8_symbol(0xe2, 0x82, 0xac)
  pound <- orpheus:::utf8_symbol(0xc2, 0xa3)

  found <- orph_find_amounts(paste0("Fee of ", euro, "500,000, plus ", pound,
                                    "300, plus $250."))
  expect_setequal(found$amount, c(500000, 300, 250))
  expect_setequal(found$currency, c("EUR", "GBP", "USD"))
  expect_true(all(found$confidence == 1.0))
})

test_that("all four ways of writing a currency reach the same result", {
  euro <- orpheus:::utf8_symbol(0xe2, 0x82, 0xac)
  for (written in c(paste0(euro, "1,000"), "EUR 1,000", "1,000 EUR", "1,000 euro")) {
    found <- orph_find_amounts(paste("Total value", written))
    expect_equal(found$amount, 1000, info = written)
    expect_equal(found$currency, "EUR", info = written)
  }
})
