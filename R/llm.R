# ---------------------------------------------------------------------------
# The two-tier model layer.
#
#   local  -- always on, runs on this server, nothing leaves the building.
#             OCR cleanup, classification, coarse entity spotting.
#   cloud  -- opt-in, per document or per batch, triggered by a person.
#             Nuanced clause reading, disambiguation, flagging.
#
# Providers are one-argument functions f(system_prompt) returning an object
# with a $chat(user_prompt) method. That is exactly the shape
# ontologyDiscoverR's `llm` argument takes, so the same provider drives both
# this package's direct calls and the population flow, with no adapter.
# ---------------------------------------------------------------------------

#' Cloud access policy values
#'
#' Which of these applies is an open deployment decision, so it is a runtime
#' setting rather than an assumption baked into the code:
#'
#' \describe{
#'   \item{`disabled`}{No cloud processing at all. The default: a deployment
#'     handling sensitive contract data should have to turn this on
#'     deliberately, not discover it was on.}
#'   \item{`per_user`}{Any authenticated user may opt a document in.}
#'   \item{`org_allow`}{Cloud is permitted, and individual calls still require
#'     an explicit per-request opt-in.}
#' }
#' @export
ORPH_CLOUD_POLICIES <- c("disabled", "per_user", "org_allow")

#' Set the LLM provider for a tier
#'
#' @param tier `"local"` or `"cloud"`.
#' @param fn A function `f(system_prompt)` returning an object with a
#'   `$chat(user_prompt)` method, or `NULL` to restore the default.
#' @return Invisibly, the previous provider.
#' @export
orph_set_llm_provider <- function(tier = c("local", "cloud"), fn) {
  tier <- match.arg(tier)
  key <- paste0("llm_", tier)
  previous <- orph_env[[key]]
  if (!is.null(fn) && !is.function(fn)) cli::cli_abort("{.arg fn} must be a function or NULL.")
  orph_env[[key]] <- fn
  invisible(previous)
}

#' The provider function for a tier
#'
#' @param tier `"local"` or `"cloud"`.
#' @return A function `f(system_prompt)`.
#' @export
orph_llm_fn <- function(tier = c("local", "cloud")) {
  tier <- match.arg(tier)
  configured <- orph_env[[paste0("llm_", tier)]]
  if (!is.null(configured)) return(configured)

  if (!requireNamespace("ellmer", quietly = TRUE)) {
    cli::cli_abort(c(
      "No {tier} model provider is configured and {.pkg ellmer} is not installed.",
      i = "Install {.pkg ellmer}, or register a provider with {.fn orph_set_llm_provider}."
    ))
  }

  if (tier == "local") {
    model <- Sys.getenv("ORPHEUS_LOCAL_MODEL", "llama3.1:8b")
    host  <- Sys.getenv("ORPHEUS_OLLAMA_HOST", "http://localhost:11434")
    function(system_prompt) ellmer::chat_ollama(model = model, system_prompt = system_prompt,
                                                base_url = host)
  } else {
    model <- Sys.getenv("ORPHEUS_CLOUD_MODEL", "claude-sonnet-4-20250514")
    function(system_prompt) ellmer::chat_anthropic(model = model, system_prompt = system_prompt)
  }
}

#' Describe the configured providers without calling them
#' @return A list.
#' @export
orph_llm_status <- function() {
  list(
    local_provider = if (!is.null(orph_env$llm_local)) "custom" else "ollama",
    local_model    = Sys.getenv("ORPHEUS_LOCAL_MODEL", "llama3.1:8b"),
    cloud_provider = if (!is.null(orph_env$llm_cloud)) "custom" else "anthropic",
    cloud_model    = Sys.getenv("ORPHEUS_CLOUD_MODEL", "claude-sonnet-4-20250514"),
    ellmer_installed = requireNamespace("ellmer", quietly = TRUE)
  )
}

# ---------------------------------------------------------------------------
# The cloud gate
# ---------------------------------------------------------------------------

#' Check whether a cloud call is permitted
#'
#' Two conditions, both required. The org policy says whether cloud processing
#' is available at all; the per-request `opt_in` says a person asked for it on
#' this document. Policy alone never authorises a call -- that is what keeps
#' cloud from becoming a silent default under an `org_allow` policy.
#'
#' @param con A connection.
#' @param opt_in Whether the request explicitly opted in.
#' @param actor_id Actor making the request.
#' @return Invisibly `TRUE`, or an error describing what is missing.
#' @export
orph_assert_cloud_allowed <- function(con, opt_in, actor_id = NULL) {
  policy <- orph_setting(con, "cloud_ai_policy", "disabled")
  if (!(policy %in% ORPH_CLOUD_POLICIES)) {
    cli::cli_abort("Stored cloud_ai_policy {.val {policy}} is not a recognised policy.")
  }
  if (identical(policy, "disabled")) {
    cli::cli_abort(c(
      "Cloud processing is disabled for this deployment.",
      i = "An administrator sets {.code cloud_ai_policy} to {.val per_user} or {.val org_allow}.",
      i = "Whether that toggle belongs to each user or to the organisation is an
           open deployment decision -- see docs/open-decisions.md."
    ))
  }
  if (!isTRUE(opt_in)) {
    cli::cli_abort(c(
      "Cloud processing needs an explicit per-request opt-in.",
      i = "Pass {.code opt_in = TRUE} (the API's {.field cloud_opt_in} field). It is
           never inferred from the policy."
    ))
  }
  invisible(TRUE)
}

#' Current cloud policy, and what it means for the caller
#' @param con A connection.
#' @return A list.
#' @export
orph_cloud_policy <- function(con) {
  policy <- orph_setting(con, "cloud_ai_policy", "disabled")
  list(
    policy    = policy,
    available = !identical(policy, "disabled"),
    send_mode = orph_setting(con, "cloud_send_mode", "excerpt"),
    requires_explicit_opt_in = TRUE
  )
}

# ---------------------------------------------------------------------------
# Calling a model
# ---------------------------------------------------------------------------

#' Call a model and parse a JSON reply
#'
#' Every call is written to `llm_calls` before returning, success or failure,
#' so the audit log records attempts rather than only outcomes.
#'
#' @param con A writable connection (for the audit row).
#' @param tier `"local"` or `"cloud"`.
#' @param system_prompt,user_prompt Prompt strings.
#' @param purpose Short label recorded in the audit log.
#' @param document_id,actor_id Recorded in the audit log.
#' @param excerpt_only Whether `user_prompt` is an excerpt rather than a whole document.
#' @param opt_in Explicit cloud opt-in; ignored for the local tier.
#' @return The parsed JSON as a list.
#' @export
orph_llm_json <- function(con, tier, system_prompt, user_prompt, purpose,
                          document_id = NULL, actor_id = NULL,
                          excerpt_only = FALSE, opt_in = FALSE) {
  tier <- match.arg(tier, c("local", "cloud"))
  if (tier == "cloud") orph_assert_cloud_allowed(con, opt_in = opt_in, actor_id = actor_id)

  status <- orph_llm_status()
  provider <- if (tier == "local") status$local_provider else status$cloud_provider
  model    <- if (tier == "local") status$local_model    else status$cloud_model

  system_prompt <- paste0(system_prompt,
    "\n\nRespond with a single valid JSON object and nothing else. ",
    "No preamble, no explanation, no markdown fences.")

  result <- tryCatch({
    chat <- orph_llm_fn(tier)(system_prompt)
    text <- chat$chat(user_prompt)
    parse_json_reply(text)
  }, error = function(e) {
    record_llm_call(con, tier, purpose, document_id, actor_id, provider, model,
                    user_prompt, excerpt_only, error = conditionMessage(e))
    cli::cli_abort(c("{tier} model call failed ({purpose}).", x = conditionMessage(e)))
  })

  record_llm_call(con, tier, purpose, document_id, actor_id, provider, model,
                  user_prompt, excerpt_only)
  result
}

#' @keywords internal
parse_json_reply <- function(text) {
  text <- paste(as.character(text), collapse = "\n")
  text <- sub("^\\s*```(json)?\\s*", "", text)
  text <- sub("\\s*```\\s*$", "", text)
  # Small models like to wrap JSON in prose. Fall back to the outermost braces
  # rather than failing the whole extraction over a stray sentence.
  parsed <- tryCatch(jsonlite::fromJSON(text, simplifyVector = FALSE), error = function(e) NULL)
  if (!is.null(parsed)) return(parsed)

  start <- regexpr("\\{", text)
  end   <- max(gregexpr("\\}", text)[[1]])
  if (start > 0 && end > start) {
    candidate <- substr(text, start, end)
    parsed <- tryCatch(jsonlite::fromJSON(candidate, simplifyVector = FALSE), error = function(e) NULL)
    if (!is.null(parsed)) return(parsed)
  }
  cli::cli_abort(c("Model did not return usable JSON.",
                   i = "First 300 characters: {.val {substr(text, 1, 300)}}"))
}

# ---------------------------------------------------------------------------
# Excerpts
# ---------------------------------------------------------------------------

#' Select the passages worth sending to a cloud model
#'
#' The architecture asks that only the relevant excerpt be sent where possible,
#' not the whole document indiscriminately. Pages are scored on how many of the
#' given terms they contain and the best ones returned, with their page numbers
#' kept so a model's answer can still cite a page.
#'
#' @param con A connection.
#' @param document_id Document identifier.
#' @param terms Character vector of terms of interest.
#' @param max_pages Maximum pages to return.
#' @param max_chars Overall character budget.
#' @return A list with `text`, `pages` and `excerpt_only`.
#' @export
orph_select_excerpts <- function(con, document_id, terms = character(),
                                 max_pages = 6L, max_chars = 24000L) {
  pages <- db_query(con,
    "SELECT page_no, text FROM document_pages WHERE document_id = ? ORDER BY page_no",
    list(document_id))
  if (nrow(pages) == 0) return(list(text = "", pages = integer(), excerpt_only = FALSE))

  scores <- rep(0, nrow(pages))
  if (length(terms) > 0) {
    lower <- tolower(pages$text %||% "")
    for (t in terms) scores <- scores + vapply(gregexpr(tolower(t), lower, fixed = TRUE),
                                               function(m) sum(m > 0), numeric(1))
  }

  # With no scoring signal, the front of a contract carries the parties, value
  # and dates, so document order is a better default than an arbitrary pick.
  order_idx <- if (all(scores == 0)) seq_len(nrow(pages)) else order(-scores, pages$page_no)
  chosen <- sort(utils::head(order_idx, max_pages))

  out <- character(); used <- integer(); budget <- max_chars
  for (i in chosen) {
    chunk <- sprintf("--- Page %d ---\n%s", pages$page_no[[i]], pages$text[[i]] %||% "")
    if (nchar(chunk) > budget) chunk <- substr(chunk, 1, max(0, budget))
    if (!nzchar(chunk)) break
    out <- c(out, chunk); used <- c(used, pages$page_no[[i]])
    budget <- budget - nchar(chunk)
    if (budget <= 0) break
  }

  list(text = paste(out, collapse = "\n\n"), pages = used,
       excerpt_only = length(used) < nrow(pages))
}
