# ---------------------------------------------------------------------------
# Pipeline step 4, deterministic half: dates and monetary values by pattern.
#
# These run before any model does. A date printed in the document is a fact
# about the text, not a judgement, so it is found by pattern and recorded at
# the top of the confidence rubric. The model is then left to do the work only
# it can do -- deciding what the date means -- rather than re-deriving findings
# a regex gets right every time.
# ---------------------------------------------------------------------------

#' @keywords internal
MONTHS <- c(january=1, february=2, march=3, april=4, may=5, june=6, july=7,
            august=8, september=9, october=10, november=11, december=12,
            jan=1, feb=2, mar=3, apr=4, jun=6, jul=7, aug=8, sep=9, sept=9,
            oct=10, nov=11, dec=12)

#' Build a currency symbol from its UTF-8 bytes
#'
#' Currency symbols cannot be written literally: R source must be ASCII to be
#' portable. But a `"\\u20ac"` escape is not equivalent either. On a server in
#' the C locale -- which is the default in a bare container -- R cannot
#' represent the euro sign natively, so it marks the escaped string UTF-8 while
#' document text read off disk stays unmarked. Matching one against the other
#' makes R attempt a translation that fails, and `orph_find_amounts()` then
#' silently returns no euro amounts at all: the worst possible failure for an
#' Irish contract, because it looks like a document with no money in it.
#'
#' Building the symbol from its raw bytes sidesteps the whole question, and the
#' symbol patterns below match with `useBytes = TRUE` so nothing is translated
#' in either direction.
#'
#' @param ... Byte values.
#' @keywords internal
utf8_symbol <- function(...) rawToChar(as.raw(c(...)))

#' @keywords internal
CURRENCY_SYMBOLS <- stats::setNames(
  c("EUR", "USD", "GBP"),
  c(utf8_symbol(0xe2, 0x82, 0xac),   # EUR sign
    "$",
    utf8_symbol(0xc2, 0xa3))         # POUND sign
)

#' Find dates in a block of text
#'
#' Handles the forms contracts actually use: ISO, `31 December 2024`,
#' `December 31, 2024`, and `31/12/2024`. Day-first is assumed for
#' slash-separated dates, which is right for Irish, UK and EU documents and
#' wrong for US ones -- so the ambiguous case is recorded at a lower rubric
#' level and its raw text kept for review.
#'
#' @param text Text to scan.
#' @return A data frame of `raw_text`, `value`, `confidence`, `ambiguous`.
#' @export
orph_find_dates <- function(text) {
  text <- as.character(text %||% "")
  out <- list()

  add <- function(raw, value, confidence, ambiguous = FALSE) {
    if (is.na(value)) return(invisible(NULL))
    out[[length(out) + 1L]] <<- data.frame(
      raw_text = raw, value = value, confidence = confidence,
      ambiguous = ambiguous, stringsAsFactors = FALSE)
  }

  for (m in regmatches(text, gregexpr("\\b\\d{4}-\\d{2}-\\d{2}\\b", text))[[1]]) {
    add(m, m, ORPH_CONFIDENCE[["explicit"]])
  }

  pat_dmy <- "\\b(\\d{1,2})(?:st|nd|rd|th)?\\s+(?i:January|February|March|April|May|June|July|August|September|October|November|December)\\,?\\s+\\d{4}\\b"
  for (m in regmatches(text, gregexpr(pat_dmy, text, perl = TRUE))[[1]]) {
    parts <- regmatches(m, regexec("(\\d{1,2})(?:st|nd|rd|th)?\\s+([A-Za-z]+)\\,?\\s+(\\d{4})", m))[[1]]
    if (length(parts) == 4) {
      mo <- MONTHS[[tolower(parts[[3]])]] %||% NA
      if (!is.na(mo)) add(m, sprintf("%s-%02d-%02d", parts[[4]], mo, as.integer(parts[[2]])),
                          ORPH_CONFIDENCE[["explicit"]])
    }
  }

  pat_mdy <- "\\b(?i:January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{1,2}(?:st|nd|rd|th)?\\,?\\s+\\d{4}\\b"
  for (m in regmatches(text, gregexpr(pat_mdy, text, perl = TRUE))[[1]]) {
    parts <- regmatches(m, regexec("([A-Za-z]+)\\s+(\\d{1,2})(?:st|nd|rd|th)?\\,?\\s+(\\d{4})", m))[[1]]
    if (length(parts) == 4) {
      mo <- MONTHS[[tolower(parts[[2]])]] %||% NA
      if (!is.na(mo)) add(m, sprintf("%s-%02d-%02d", parts[[4]], mo, as.integer(parts[[3]])),
                          ORPH_CONFIDENCE[["explicit"]])
    }
  }

  for (m in regmatches(text, gregexpr("\\b\\d{1,2}[/.]\\d{1,2}[/.]\\d{4}\\b", text))[[1]]) {
    parts <- as.integer(strsplit(m, "[/.]")[[1]])
    # Day-first unless the first field cannot be a day.
    ambiguous <- parts[[1]] <= 12 && parts[[2]] <= 12
    if (parts[[1]] > 12) { d <- parts[[1]]; mo <- parts[[2]] } else { d <- parts[[1]]; mo <- parts[[2]] }
    if (mo >= 1 && mo <= 12 && d >= 1 && d <= 31) {
      add(m, sprintf("%d-%02d-%02d", parts[[3]], mo, d),
          if (ambiguous) ORPH_CONFIDENCE[["inferred"]] else ORPH_CONFIDENCE[["explicit"]],
          ambiguous = ambiguous)
    }
  }

  if (length(out) == 0) {
    return(data.frame(raw_text = character(), value = character(),
                      confidence = numeric(), ambiguous = logical(),
                      stringsAsFactors = FALSE))
  }
  df <- do.call(rbind, out)
  df[!duplicated(df$raw_text), , drop = FALSE]
}

#' Find monetary amounts in a block of text
#'
#' @param text Text to scan.
#' @return A data frame of `raw_text`, `amount`, `currency`, `confidence`.
#' @export
orph_find_amounts <- function(text) {
  text <- as.character(text %||% "")
  num <- "[0-9]{1,3}(?:,[0-9]{3})*(?:\\.[0-9]+)?|[0-9]+(?:\\.[0-9]+)?"
  out <- list()

  add <- function(raw, amount, currency, confidence) {
    if (is.na(amount)) return(invisible(NULL))
    out[[length(out) + 1L]] <<- data.frame(
      raw_text = raw, amount = amount, currency = currency,
      confidence = confidence, stringsAsFactors = FALSE)
  }
  to_number <- function(s) suppressWarnings(as.numeric(gsub(",", "", s)))

  # Symbol-prefixed. useBytes = TRUE throughout: see utf8_symbol() above.
  for (sym in names(CURRENCY_SYMBOLS)) {
    pat <- paste0("\\Q", sym, "\\E\\s?(?:", num, ")")
    for (m in regmatches(text, gregexpr(pat, text, perl = TRUE, useBytes = TRUE))[[1]]) {
      add(m, to_number(gsub(paste0("\\Q", sym, "\\E|\\s"), "", m, perl = TRUE,
                            useBytes = TRUE)),
          CURRENCY_SYMBOLS[[sym]], ORPH_CONFIDENCE[["explicit"]])
    }
  }

  # Code-prefixed or code-suffixed: EUR 2,400,000 / 2,400,000 EUR
  for (m in regmatches(text, gregexpr(paste0("\\b(?:EUR|USD|GBP)\\s?(?:", num, ")"), text, perl = TRUE))[[1]]) {
    code <- toupper(substr(gsub("^\\s+", "", m), 1, 3))
    add(m, to_number(gsub("[A-Za-z\\s]", "", m, perl = TRUE)), code, ORPH_CONFIDENCE[["explicit"]])
  }
  for (m in regmatches(text, gregexpr(paste0("(?:", num, ")\\s?(?:EUR|USD|GBP)\\b"), text, perl = TRUE))[[1]]) {
    code <- toupper(regmatches(m, regexpr("(EUR|USD|GBP)", m))[[1]])
    add(m, to_number(gsub("[A-Za-z\\s]", "", m, perl = TRUE)), code, ORPH_CONFIDENCE[["explicit"]])
  }

  # Written out: "2,400,000 euro". Lower rubric level -- the currency is being
  # read from a word next to the number rather than from a code.
  for (m in regmatches(text, gregexpr(paste0("(?:", num, ")\\s?(?i:euros?|dollars?|pounds?)\\b"), text, perl = TRUE))[[1]]) {
    word <- tolower(regmatches(m, regexpr("(?i:euros?|dollars?|pounds?)", m, perl = TRUE))[[1]])
    code <- if (grepl("^euro", word)) "EUR" else if (grepl("^dollar", word)) "USD" else "GBP"
    add(m, to_number(gsub("[A-Za-z\\s]", "", m, perl = TRUE)), code, ORPH_CONFIDENCE[["named"]])
  }

  if (length(out) == 0) {
    return(data.frame(raw_text = character(), amount = numeric(),
                      currency = character(), confidence = numeric(),
                      stringsAsFactors = FALSE))
  }
  df <- do.call(rbind, out)
  df <- df[!is.na(df$amount), , drop = FALSE]
  df[!duplicated(df$raw_text), , drop = FALSE]
}

#' Cues that say what a date is for
#'
#' Matched as literal substrings, so a cue must be written as a **stem** to
#' survive ordinary inflection: `"commence"` catches commences and commencing,
#' where `"terminate on"` caught only the bare infinitive and missed
#' "terminates on" -- the commonest phrasing there is. A date that matches no
#' cue is left `unknown` rather than guessed at, but one that matches the wrong
#' cue is worse than either, because it reads as a fact.
#'
#' @keywords internal
DATE_ROLE_CUES <- list(
  start     = c("commenc", "start date", "effective from", "effective date", "with effect from"),
  end       = c("expir", "terminat", "end date", "until", "ceases"),
  signature = c("signed", "executed", "dated this", "in witness whereof"),
  milestone = c("milestone", "delivery date", "by no later than", "deadline")
)

#' @keywords internal
AMOUNT_ROLE_CUES <- list(
  contract_value = c("total value", "contract value", "consideration", "contract sum", "total price"),
  cap            = c("aggregate liability", "shall not exceed", "capped at", "maximum liability"),
  penalty        = c("liquidated damages", "penalty", "service credit"),
  rate           = c("per day", "per hour", "per annum", "day rate", "hourly rate")
)

#' @keywords internal
infer_role <- function(text, position, cues, window = 160L) {
  if (is.na(position) || position < 1) return("unknown")
  start  <- max(1, position - window)
  around <- tolower(substr(text, start, min(nchar(text), position + window)))
  # Offset of the value itself within the window, so distances are measured
  # from the date or amount rather than from the window's edge.
  target <- position - start + 1L

  best_role <- "unknown"; best_dist <- Inf
  for (role in names(cues)) {
    for (cue in cues[[role]]) {
      hits <- gregexpr(cue, around, fixed = TRUE)[[1]]
      if (hits[[1]] < 0) next
      # A cue introduces the value that follows it, so a cue before the value
      # is the one that governs it. Cues after it are still considered, at a
      # penalty, for the trailing forms ("2026-12-31, the expiry date").
      for (h in hits) {
        dist <- if (h <= target) target - h else (h - target) * 2L
        if (dist < best_dist) { best_dist <- dist; best_role <- role }
      }
    }
  }
  best_role
}
