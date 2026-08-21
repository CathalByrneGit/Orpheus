# ---------------------------------------------------------------------------
# The ontology bundle: loading, validation, registration, and the DDL it implies.
#
# The bundle is the schema. Instance tables are generated from it rather than
# hand-written, so accepting a schema amendment or swapping in a bundle
# produced by a real ontologyDiscoverR discovery run changes the store's shape
# without a migration being written by hand.
# ---------------------------------------------------------------------------

#' Path to the bundle shipped with the package
#' @export
orph_default_bundle_path <- function() {
  p <- system.file("bundles", "contract-core-0.1.0.json", package = "orpheus")
  if (nzchar(p)) return(p)
  # Running from a source checkout without installing.
  file.path("inst", "bundles", "contract-core-0.1.0.json")
}

#' Load an ontology bundle from JSON
#'
#' @param path Path to a bundle JSON file. Defaults to the bundle shipped with
#'   the package.
#' @return A bundle list.
#' @export
orph_load_bundle <- function(path = orph_default_bundle_path()) {
  if (!file.exists(path)) cli::cli_abort("No bundle at {.path {path}}.")
  bundle <- jsonlite::fromJSON(path, simplifyVector = FALSE)
  orph_validate_bundle(bundle)
  bundle
}

#' Validate that a bundle carries what its three consumers read
#'
#' `ontologyDiscoverR`, `conceptR` and `objectSetsR` each read different keys
#' for the same concepts. A bundle missing one spelling fails at the point of
#' use, deep inside whichever package needed it, so it is checked up front.
#'
#' @param bundle A bundle list.
#' @return The bundle, invisibly.
#' @export
orph_validate_bundle <- function(bundle) {
  problems <- character()

  if (is.null(bundle$object_types) || length(bundle$object_types) == 0) {
    problems <- c(problems, "bundle has no object_types")
  }
  if (is.null(bundle$bundle_id)) problems <- c(problems, "bundle has no bundle_id")
  if (is.null(bundle$version))   problems <- c(problems, "bundle has no version")

  type_ids <- character()
  for (ot in bundle$object_types %||% list()) {
    id <- ot$id %||% "<unnamed>"
    type_ids <- c(type_ids, id)
    if (is.null(ot$table_name))
      problems <- c(problems, sprintf("object type '%s': missing table_name (conceptR reads it)", id))
    if (is.null(ot$primary_key))
      problems <- c(problems, sprintf("object type '%s': missing primary_key (conceptR reads it)", id))
    if (is.null(ot$source$table))
      problems <- c(problems, sprintf("object type '%s': missing source$table (objectSetsR reads it)", id))
    if (!identical(ot$source$table, ot$table_name))
      problems <- c(problems, sprintf("object type '%s': table_name and source$table disagree", id))
    if (is.null(ot$properties) || length(ot$properties) == 0)
      problems <- c(problems, sprintf("object type '%s': has no properties", id))
    prop_ids <- vapply(ot$properties %||% list(), function(p) p$id %||% "", character(1))
    if (!(ot$primary_key %in% prop_ids))
      problems <- c(problems, sprintf("object type '%s': primary_key '%s' is not a declared property",
                                      id, ot$primary_key))
    if (!("status" %in% prop_ids))
      problems <- c(problems, sprintf(
        "object type '%s': 'status' is not a declared property, so object sets could not exclude rejected rows", id))
  }

  for (lt in bundle$link_types %||% list()) {
    id <- lt$id %||% "<unnamed>"
    if (is.null(lt$from) || is.null(lt$to))
      problems <- c(problems, sprintf("link type '%s': missing from/to (objectSetsR reads them)", id))
    if (is.null(lt$from_type_id) || is.null(lt$to_type_id))
      problems <- c(problems, sprintf("link type '%s': missing from_type_id/to_type_id (ontologyDiscoverR reads them)", id))
    if (!identical(lt$from, lt$from_type_id) || !identical(lt$to, lt$to_type_id))
      problems <- c(problems, sprintf("link type '%s': from/to and from_type_id/to_type_id disagree", id))
    for (side in c("from", "to")) {
      tid <- lt[[side]]
      if (!is.null(tid) && !(tid %in% type_ids))
        problems <- c(problems, sprintf("link type '%s': %s references unknown object type '%s'",
                                        id, side, tid))
    }
    if (is.null(lt$join$fromKeys) || is.null(lt$join$toKeys))
      problems <- c(problems, sprintf("link type '%s': missing join keys (objectSetsR traversal needs them)", id))
  }

  if (length(problems) > 0) {
    cli::cli_abort(c("Bundle is not valid:", stats::setNames(problems, rep("x", length(problems)))))
  }
  invisible(bundle)
}

#' Look up an object type in a bundle
#' @param bundle A bundle.
#' @param type_id Object type identifier.
#' @return The object type list, or `NULL`.
#' @export
orph_object_type <- function(bundle, type_id) {
  for (ot in bundle$object_types %||% list()) if (identical(ot$id, type_id)) return(ot)
  NULL
}

#' Look up a link type in a bundle
#' @param bundle A bundle.
#' @param link_type_id Link type identifier.
#' @return The link type list, or `NULL`.
#' @export
orph_link_type <- function(bundle, link_type_id) {
  for (lt in bundle$link_types %||% list()) if (identical(lt$id, link_type_id)) return(lt)
  NULL
}

#' Property identifiers of an object type
#' @param object_type An object type list.
#' @return Character vector.
#' @export
orph_property_ids <- function(object_type) {
  vapply(object_type$properties %||% list(), function(p) p$id %||% "", character(1))
}

#' Object types whose tables Orpheus generates
#' @param bundle A bundle.
#' @return List of object types.
#' @keywords internal
managed_object_types <- function(bundle) {
  Filter(function(ot) !identical(ot$x_orpheus$managed, FALSE), bundle$object_types %||% list())
}

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

#' @keywords internal
sql_type_for <- function(prop_type) {
  switch(prop_type %||% "string",
    integer = "INTEGER",
    double  = "REAL",
    numeric = "REAL",
    boolean = "INTEGER",
    "TEXT")
}

#' Create the instance tables a bundle implies
#'
#' One table per managed object type, with a column per declared property.
#' Idempotent: existing tables gain any columns the bundle has added since,
#' which is what makes accepting a schema amendment a live operation rather
#' than a redeployment.
#'
#' @param con A writable connection.
#' @param bundle A bundle.
#' @return Invisibly, a character vector of tables touched.
#' @export
orph_apply_bundle_schema <- function(con, bundle) {
  assert_writable(con)
  touched <- character()

  for (ot in managed_object_types(bundle)) {
    table <- ot$table_name
    pk    <- ot$primary_key
    props <- ot$properties %||% list()

    if (!DBI::dbExistsTable(con, table)) {
      cols <- vapply(props, function(p) {
        sprintf("%s %s%s",
                DBI::dbQuoteIdentifier(con, p$id),
                sql_type_for(p$type),
                if (identical(p$id, pk)) " PRIMARY KEY" else "")
      }, character(1))
      # created_at is not a bundle property: it is store bookkeeping, and
      # declaring it as a property would put it in every object set projection.
      cols <- c(cols, "created_at TEXT")
      DBI::dbExecute(con, sprintf("CREATE TABLE %s (%s)",
                                  DBI::dbQuoteIdentifier(con, table),
                                  paste(cols, collapse = ", ")))
      DBI::dbExecute(con, sprintf(
        "CREATE INDEX IF NOT EXISTS %s ON %s (document_id)",
        DBI::dbQuoteIdentifier(con, paste0("idx_", table, "_doc")),
        DBI::dbQuoteIdentifier(con, table)))
      touched <- c(touched, table)
    } else {
      existing <- DBI::dbListFields(con, table)
      for (p in props) {
        if (!(p$id %in% existing)) {
          DBI::dbExecute(con, sprintf("ALTER TABLE %s ADD COLUMN %s %s",
                                      DBI::dbQuoteIdentifier(con, table),
                                      DBI::dbQuoteIdentifier(con, p$id),
                                      sql_type_for(p$type)))
          touched <- c(touched, table)
        }
      }
    }
  }
  invisible(unique(touched))
}

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

#' Register a bundle in the store and apply its schema
#'
#' @param con A writable connection.
#' @param bundle A bundle list.
#' @param actor_id Actor registering the bundle.
#' @param activate Make this the active bundle.
#' @param stage `"production"` or `"staging"`. Staging bundles are stored and
#'   inspectable but are never activated implicitly -- the hook for the
#'   staging-vs-production split flagged in the roadmap.
#' @return Invisibly, the bundle.
#' @export
orph_register_bundle <- function(con, bundle, actor_id = NULL, activate = TRUE,
                                 stage = c("production", "staging")) {
  assert_writable(con)
  stage <- match.arg(stage)
  orph_validate_bundle(bundle)
  if (stage == "staging" && activate) {
    cli::cli_abort("A staging bundle cannot be activated. Promote it to production first.")
  }

  with_tx(con, {
    DBI::dbExecute(con,
      "INSERT INTO bundles (bundle_id, bundle_version, bundle_json, stage, created_at, created_by, is_active)
       VALUES (?, ?, ?, ?, ?, ?, 0)
       ON CONFLICT(bundle_id, bundle_version) DO UPDATE SET
         bundle_json = excluded.bundle_json, stage = excluded.stage",
      params = list(bundle$bundle_id, bundle$version,
                    as.character(to_json(bundle)), stage, orph_now(), nullable(actor_id)))

    if (activate) {
      DBI::dbExecute(con, "UPDATE bundles SET is_active = 0")
      DBI::dbExecute(con, "UPDATE bundles SET is_active = 1 WHERE bundle_id = ? AND bundle_version = ?",
                     params = list(bundle$bundle_id, bundle$version))
      orph_apply_bundle_schema(con, bundle)
    }
  })
  invisible(bundle)
}

#' Read the active bundle
#' @param con A connection.
#' @return The active bundle list, or `NULL` if none is active.
#' @export
orph_active_bundle <- function(con) {
  row <- db_get_one(con, "SELECT bundle_json FROM bundles WHERE is_active = 1 LIMIT 1")
  if (is.null(row)) return(NULL)
  from_json(row$bundle_json)
}

#' Initialise a store: migrations plus the default bundle
#'
#' @param path Path to the SQLite file.
#' @param bundle Bundle to register. Defaults to the shipped contract bundle.
#' @param force_lock Take over a stale writer lock.
#' @return An open writable connection.
#' @export
orph_init_store <- function(path, bundle = orph_load_bundle(), force_lock = FALSE) {
  con <- orph_connect(path, mode = "write", force_lock = force_lock)
  orph_register_bundle(con, bundle, activate = TRUE)
  con
}
