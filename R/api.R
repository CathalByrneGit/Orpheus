# ---------------------------------------------------------------------------
# The Plumber HTTP API.
#
# This process is the single writer. Datasette and any UI are read-only clients
# against the same file; every mutation in the system enters here, which is
# what makes the single-writer constraint hold in practice rather than by
# convention.
#
# The router is built programmatically rather than from annotations so it can
# be constructed and exercised in tests without binding a port.
# ---------------------------------------------------------------------------

#' @keywords internal
api_state <- new.env(parent = emptyenv())

#' @keywords internal
json_error <- function(res, status, message, detail = NULL) {
  res$status <- status
  list(error = list(status = status, message = message, detail = detail))
}

#' @keywords internal
body_of <- function(req) {
  if (!is.null(req$argsBody) && length(req$argsBody)) return(req$argsBody)
  raw <- req$postBody %||% ""
  if (!nzchar(raw)) return(list())
  tryCatch(jsonlite::fromJSON(raw, simplifyVector = FALSE), error = function(e) list())
}

#' @keywords internal
param <- function(req, name, default = NULL) {
  body <- body_of(req)
  body[[name]] %||% req$args[[name]] %||% (req$argsQuery %||% list())[[name]] %||% default
}

#' @keywords internal
as_flag <- function(x, default = FALSE) {
  if (is.null(x)) return(default)
  if (is.logical(x)) return(isTRUE(x[[1]]))
  tolower(as.character(x)[[1]]) %in% c("true", "1", "yes")
}

#' @keywords internal
require_actor <- function(req, res) {
  if (is.null(req$actor)) {
    res$status <- 401L
    return(NULL)
  }
  req$actor
}

#' @keywords internal
guard <- function(res, expr) {
  tryCatch(force(expr), error = function(e) {
    if (inherits(e, "orph_forbidden")) return(json_error(res, 403L, conditionMessage(e)))
    json_error(res, 400L, conditionMessage(e))
  })
}

#' Build the Orpheus API router
#'
#' @param db_path Path to the SQLite store.
#' @param storage_root Directory for originals and page images.
#' @param force_lock Take over a stale writer lock left by a crashed process.
#' @return A `plumber` router.
#' @export
orph_api <- function(db_path = Sys.getenv("ORPHEUS_DB", "data/orpheus.sqlite"),
                     storage_root = Sys.getenv("ORPHEUS_STORAGE", "storage"),
                     force_lock = FALSE) {
  if (!requireNamespace("plumber", quietly = TRUE)) {
    cli::cli_abort("{.pkg plumber} is required to serve the API.")
  }

  con <- if (file.exists(db_path)) {
    orph_connect(db_path, mode = "write", force_lock = force_lock)
  } else {
    orph_init_store(db_path, force_lock = force_lock)
  }
  api_state$con <- con
  api_state$storage_root <- storage_root

  pr <- plumber::pr()

  # R has no scalar type, so plumber's default JSON serializer renders every
  # single value as a one-element array ({"status":["ok"]}). Setting the router
  # default does not fix it: pr_get()/pr_post() bind a serializer to each
  # endpoint as it is registered, so by the time a router default is set every
  # route already has one. It has to be passed per route, which these helpers do.
  unboxed <- plumber::serializer_unboxed_json()
  GET  <- function(pr, path, handler) plumber::pr_get(pr, path, handler, serializer = unboxed)
  POST <- function(pr, path, handler) plumber::pr_post(pr, path, handler, serializer = unboxed)

  # --- authentication -------------------------------------------------------
  pr <- plumber::pr_filter(pr, "auth", function(req, res) {
    header <- req$HTTP_AUTHORIZATION %||% ""
    token  <- if (grepl("^Bearer ", header)) sub("^Bearer ", "", header) else ""
    req$actor <- if (nzchar(token)) orph_authenticate(con, token) else NULL
    plumber::forward()
  })

  # --- open endpoints -------------------------------------------------------
  pr <- GET(pr, "/health", function() {
    list(status = "ok", service = "orpheus", phase = "1", time = orph_now())
  })

  pr <- GET(pr, "/capabilities", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    bundle <- orph_active_bundle(con)
    list(text_extraction = orph_extraction_capabilities(),
         models = orph_llm_status(),
         cloud = orph_cloud_policy(con),
         bundle = list(bundle_id = bundle$bundle_id, version = bundle$version,
                       object_types = length(bundle$object_types %||% list()),
                       link_types = length(bundle$link_types %||% list())),
         confidence_rubric = as.list(ORPH_CONFIDENCE))
  })

  pr <- GET(pr, "/bundle", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    orph_active_bundle(con)
  })

  # --- documents ------------------------------------------------------------
  pr <- GET(pr, "/documents", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    orph_visible_documents(con, actor, limit = as.integer(param(req, "limit", 100L)))
  })

  pr <- POST(pr, "/documents", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      upload <- extract_upload(req)
      if (is.null(upload)) {
        return(json_error(res, 400L, "No file supplied.",
                          "Send a multipart upload, or a JSON body with a server-local 'path'."))
      }
      on.exit(if (isTRUE(upload$temporary)) unlink(upload$path), add = TRUE)
      result <- orph_ingest(con, upload$path, actor_id = actor$actor_id,
                            storage_root = storage_root, filename = upload$filename,
                            visibility = param(req, "visibility", "private"))
      if (isTRUE(result$duplicate)) res$status <- 200L else res$status <- 201L
      result
    })
  })

  pr <- GET(pr, "/documents/<id>", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "view")
      c(orph_get_document(con, id), list(review = orph_review_progress(con, id)))
    })
  })

  pr <- GET(pr, "/documents/<id>/text", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "view")
      list(document_id = id, text = orph_document_text(con, id),
           pages = db_query(con, "SELECT page_no, text_source, char_count
                                  FROM document_pages WHERE document_id = ? ORDER BY page_no",
                            list(id)))
    })
  })

  pr <- POST(pr, "/documents/<id>/classify", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, { orph_require(con, actor, id, "edit"); orph_classify(con, id, actor$actor_id) })
  })

  pr <- POST(pr, "/documents/<id>/extract", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "edit")
      orph_extract(con, id, tier = param(req, "tier", "local"), actor_id = actor$actor_id,
                   opt_in = as_flag(param(req, "cloud_opt_in")),
                   deterministic = as_flag(param(req, "deterministic"), TRUE))
    })
  })

  pr <- GET(pr, "/documents/<id>/instances", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "view")
      orph_document_instances(con, id, type_id = param(req, "type_id"),
                              include_rejected = as_flag(param(req, "include_rejected")))
    })
  })

  pr <- GET(pr, "/documents/<id>/edges", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "view")
      db_query(con, "SELECT * FROM edges WHERE document_id = ? ORDER BY created_at", list(id))
    })
  })

  pr <- GET(pr, "/documents/<id>/history", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, { orph_require(con, actor, id, "view"); orph_document_history(con, id) })
  })

  pr <- GET(pr, "/documents/<id>/review", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, { orph_require(con, actor, id, "view"); orph_review_progress(con, id) })
  })

  pr <- POST(pr, "/documents/<id>/review", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "edit")
      orph_mark_document_reviewed(con, id, actor$actor_id,
                                  reviewed = as_flag(param(req, "reviewed"), TRUE))
    })
  })

  # --- sharing --------------------------------------------------------------
  pr <- POST(pr, "/documents/<id>/share", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      target <- param(req, "actor_id")
      if (is.null(target)) return(json_error(res, 400L, "actor_id is required."))
      if (as_flag(param(req, "revoke"))) {
        orph_unshare_document(con, id, target, revoked_by = actor$actor_id)
        list(document_id = id, actor_id = target, shared = FALSE)
      } else {
        role <- param(req, "role", "viewer")
        orph_share_document(con, id, target, role, granted_by = actor$actor_id)
        list(document_id = id, actor_id = target, role = role, shared = TRUE)
      }
    })
  })

  pr <- POST(pr, "/documents/<id>/visibility", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      vis <- param(req, "visibility")
      if (is.null(vis)) return(json_error(res, 400L, "visibility is required."))
      orph_set_visibility(con, id, vis, actor$actor_id)
      list(document_id = id, visibility = vis)
    })
  })

  # --- instance review ------------------------------------------------------
  pr <- POST(pr, "/instances/<id>/confirm", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      loc <- locate_instance(con, id)
      orph_require(con, actor, loc$document_id, "edit")
      orph_confirm_instance(con, id, actor$actor_id)
      list(instance_id = id, status = "confirmed")
    })
  })

  pr <- POST(pr, "/instances/<id>/amend", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      loc <- locate_instance(con, id)
      orph_require(con, actor, loc$document_id, "edit")
      changes <- param(req, "changes")
      if (is.null(changes) || !length(changes)) {
        return(json_error(res, 400L, "changes is required and must be a non-empty object."))
      }
      orph_amend_instance(con, id, changes, actor$actor_id, note = param(req, "note"))
      list(instance_id = id, status = "amended")
    })
  })

  pr <- POST(pr, "/instances/<id>/reject", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      loc <- locate_instance(con, id)
      orph_require(con, actor, loc$document_id, "edit")
      orph_reject_instance(con, id, actor$actor_id, note = param(req, "note"))
      list(instance_id = id, status = "rejected")
    })
  })

  pr <- GET(pr, "/instances/<id>/history", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      loc <- locate_instance(con, id)
      orph_require(con, actor, loc$document_id, "view")
      orph_row_history(con, loc$table_name, id)
    })
  })

  pr <- POST(pr, "/edges/<id>/review", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      edge <- db_get_one(con, "SELECT document_id FROM edges WHERE edge_id = ?", list(id))
      if (is.null(edge)) return(json_error(res, 404L, "No such edge."))
      orph_require(con, actor, edge$document_id, "edit")
      orph_review_edge(con, id, status = param(req, "status", "confirmed"),
                       actor_id = actor$actor_id, link_type_id = param(req, "link_type_id"),
                       note = param(req, "note"))
      list(edge_id = id, status = param(req, "status", "confirmed"))
    })
  })

  # --- schema amendment queue ----------------------------------------------
  pr <- GET(pr, "/schema-amendments", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    orph_schema_amendments(con, status = param(req, "status", "pending"))
  })

  pr <- POST(pr, "/schema-amendments/<id>/review", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    # Accepting one changes the bundle for everyone, so it is an administrator
    # action rather than an ordinary review.
    if (!isTRUE(actor$is_admin)) return(json_error(res, 403L,
      "Only an administrator can decide a schema amendment.",
      "Accepting one changes the ontology bundle for every document, not one row."))
    guard(res, orph_review_schema_amendment(con, id, decision = param(req, "decision", "accepted"),
                                            actor_id = actor$actor_id, note = param(req, "note")))
  })

  # --- analysis -------------------------------------------------------------
  pr <- POST(pr, "/documents/<id>/concepts/evaluate", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "edit")
      list(document_id = id, concepts = orph_evaluate_concepts(con, id, actor$actor_id))
    })
  })

  pr <- POST(pr, "/documents/<id>/analyse", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "edit")
      orph_analyse_document(con, id, tier = param(req, "tier", "cloud"),
                            actor_id = actor$actor_id,
                            opt_in = as_flag(param(req, "cloud_opt_in")))
    })
  })

  pr <- GET(pr, "/documents/<id>/evaluations", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "view")
      orph_document_evaluations(con, id, kind = param(req, "kind"),
                                include_stale = as_flag(param(req, "include_stale"), TRUE))
    })
  })

  pr <- POST(pr, "/evaluations/<id>/review", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      ev <- db_get_one(con, "SELECT target_document_id FROM concept_evaluations WHERE evaluation_id = ?",
                       list(id))
      if (is.null(ev)) return(json_error(res, 404L, "No such evaluation."))
      orph_require(con, actor, ev$target_document_id, "edit")
      orph_review_evaluation(con, id, status = param(req, "status", "confirmed"),
                             actor_id = actor$actor_id, result = param(req, "result"),
                             note = param(req, "note"))
      list(evaluation_id = id, status = param(req, "status", "confirmed"))
    })
  })

  pr <- POST(pr, "/documents/<id>/corpus-analysis", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "view")
      orph_corpus_analysis(con, id, actor_id = actor$actor_id,
                           narrate = as_flag(param(req, "narrate")),
                           tier = param(req, "tier", "cloud"),
                           opt_in = as_flag(param(req, "cloud_opt_in")))
    })
  })

  # --- scores and concept parameters -----------------------------------------
  pr <- POST(pr, "/documents/<id>/score", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "edit")
      orph_evaluate_score(con, id, score_id = param(req, "score_id"),
                          actor_id = actor$actor_id)
    })
  })

  pr <- GET(pr, "/documents/<id>/risk", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, { orph_require(con, actor, id, "view"); orph_risk_comparison(con, id) })
  })

  pr <- GET(pr, "/concept-parameters", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, orph_concept_parameters(con))
  })

  pr <- POST(pr, "/admin/concept-parameters", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    # Changing a threshold changes what every document is measured against, so
    # it is an administrator action even though it looks like a setting.
    if (!isTRUE(actor$is_admin)) return(json_error(res, 403L, "Administrator only."))
    guard(res, {
      template_id <- param(req, "template_id"); parameter <- param(req, "parameter")
      value <- param(req, "value")
      if (is.null(template_id) || is.null(parameter) || is.null(value)) {
        return(json_error(res, 400L, "template_id, parameter and value are all required."))
      }
      orph_set_concept_parameter(con, template_id, parameter, value, actor$actor_id)
      list(template_id = template_id, parameter = parameter, value = value,
           note = "A new concept version was created; re-evaluate affected documents.")
    })
  })

  pr <- POST(pr, "/admin/scores/setup", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    if (!isTRUE(actor$is_admin)) return(json_error(res, 403L, "Administrator only."))
    guard(res, orph_setup_scores(con, actor_id = actor$actor_id))
  })

  # --- extraction quality ---------------------------------------------------
  pr <- GET(pr, "/quality", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    # Corpus-wide figures span documents this actor may not be able to read, so
    # they are aggregate-only and restricted to administrators. A per-document
    # report is available to anyone who can view that document.
    if (!isTRUE(actor$is_admin)) return(json_error(res, 403L,
      "Corpus-wide quality figures are administrator only.",
      "Use /documents/<id>/quality for a document you can view."))
    guard(res, orph_quality_report(con,
      min_reviewed = as.integer(param(req, "min_reviewed", 5L))))
  })

  pr <- GET(pr, "/documents/<id>/quality", function(req, res, id) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    guard(res, {
      orph_require(con, actor, id, "view")
      orph_quality_report(con, document_id = id,
                          min_reviewed = as.integer(param(req, "min_reviewed", 1L)))
    })
  })

  # --- audit and administration --------------------------------------------
  pr <- GET(pr, "/audit/llm", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    if (!isTRUE(actor$is_admin)) return(json_error(res, 403L, "Administrator only."))
    orph_llm_audit(con, document_id = param(req, "document_id"), tier = param(req, "tier"))
  })

  pr <- POST(pr, "/admin/settings", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    if (!isTRUE(actor$is_admin)) return(json_error(res, 403L, "Administrator only."))
    guard(res, {
      key <- param(req, "key"); value <- param(req, "value")
      if (is.null(key)) return(json_error(res, 400L, "key is required."))
      if (identical(key, "cloud_ai_policy")) assert_choice(value, ORPH_CLOUD_POLICIES, "value")
      orph_set_setting(con, key, value, actor$actor_id)
      list(key = key, value = value)
    })
  })

  pr <- POST(pr, "/admin/concepts/setup", function(req, res) {
    actor <- require_actor(req, res); if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
    if (!isTRUE(actor$is_admin)) return(json_error(res, 403L, "Administrator only."))
    guard(res, orph_setup_concepts(con, actor_id = actor$actor_id))
  })

  # Fold the WAL back into the main file after anything that wrote. Readers --
  # Datasette, backups, a second read connection -- see committed data only up
  # to the last checkpoint if they opened the file immutable, and the WAL grows
  # without bound if nothing ever checkpoints it.
  pr <- plumber::pr_hook(pr, "postroute", function(req, res) {
    if (!identical(req$REQUEST_METHOD, "GET")) try(orph_checkpoint(con), silent = TRUE)
  })

  pr <- plumber::pr_hook(pr, "exit", function() {
    try(orph_checkpoint(con, "TRUNCATE"), silent = TRUE)
    try(orph_disconnect(con), silent = TRUE)
  })

  pr
}

#' Pull an uploaded file out of a request
#'
#' Accepts a multipart upload, or a JSON body naming a path already on the
#' server -- which is how a watched drop-directory or a batch load feeds the
#' same code path as a browser upload.
#'
#' @keywords internal
extract_upload <- function(req) {
  parsed <- req$body
  if (!is.null(parsed) && length(parsed)) {
    for (nm in names(parsed)) {
      part <- parsed[[nm]]
      if (is.list(part) && !is.null(part$filename) && !is.null(part$value)) {
        tmp <- file.path(tempdir(), basename(part$filename))
        writeBin(part$value, tmp)
        return(list(path = tmp, filename = basename(part$filename), temporary = TRUE))
      }
      if (is.raw(part)) {
        fn <- (parsed$filename %||% "upload.pdf")
        tmp <- file.path(tempdir(), basename(as.character(fn)))
        writeBin(part, tmp)
        return(list(path = tmp, filename = basename(as.character(fn)), temporary = TRUE))
      }
    }
  }
  body <- body_of(req)
  if (!is.null(body$path) && file.exists(as.character(body$path))) {
    return(list(path = as.character(body$path),
                filename = body$filename %||% basename(as.character(body$path)),
                temporary = FALSE))
  }
  NULL
}
