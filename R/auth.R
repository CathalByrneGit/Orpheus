# ---------------------------------------------------------------------------
# Identity and permissions.
#
# The actor model follows Datasette's: every request carries an actor, and
# access is decided from that actor plus the resource. Per-document access
# follows datasette-paper's pattern -- an owner, a visibility level, and a
# share table naming actors and their role -- because that pattern already
# solves the row-level, per-document problem this app has, and mirroring it
# keeps the API and Datasette answering the same question the same way.
#
# What is deliberately NOT invented here: department-scoped or
# sensitivity-tagged rules. Which boundaries actually matter is an open
# question for stakeholders, and guessing would bake a rule into the schema
# that then has to be unpicked. Actors carry their departments so a
# datasette-acl dynamic group can key off them the moment those rules exist.
# ---------------------------------------------------------------------------

#' Roles a document share can grant
#' @export
ORPH_SHARE_ROLES <- c("viewer", "editor")

#' Document visibility levels
#' @export
ORPH_VISIBILITY <- c("private", "link-view", "link-edit")

#' Actions permissions are checked against
#' @export
ORPH_ACTIONS <- c("view", "edit", "share", "delete")

#' Create an actor
#'
#' @param con A writable connection.
#' @param display_name Human-readable name.
#' @param email Email address.
#' @param idp Identity provider key (e.g. `"entra"`, `"okta"`, `"github"`).
#' @param external_id The actor's id at that provider.
#' @param departments Character vector of departments, for dynamic groups.
#' @param is_admin Whether this actor administers the deployment.
#' @param actor_id Explicit id, otherwise generated.
#' @return The actor id.
#' @export
orph_create_actor <- function(con, display_name, email = NULL, idp = NULL,
                              external_id = NULL, departments = character(),
                              is_admin = FALSE, actor_id = NULL) {
  assert_writable(con)
  assert_string(display_name, "display_name")
  id <- actor_id %||% orph_id("act")
  db_insert(con, "actors", list(
    actor_id = id, display_name = display_name, email = nullable(email),
    idp = nullable(idp), external_id = nullable(external_id),
    departments_json = as.character(to_json(as.list(departments))),
    is_admin = as.integer(isTRUE(is_admin)), created_at = orph_now()))
  record_edit(con, "actors", id, NULL, "actor_created", NULL,
              list(display_name = display_name, idp = idp, is_admin = isTRUE(is_admin)),
              actor_id = id)
  id
}

#' Find or create the actor behind an external identity
#'
#' The bridge from whichever identity provider the deployment settles on. The
#' provider is not chosen here -- an OIDC, Entra, Okta or GitHub plugin all
#' arrive at the same place: an `idp` and an `external_id`.
#'
#' @param con A writable connection.
#' @param idp Identity provider key.
#' @param external_id The actor's id at that provider.
#' @param display_name,email Used when creating the actor.
#' @param departments Character vector of departments.
#' @return The actor id.
#' @export
orph_upsert_actor <- function(con, idp, external_id, display_name, email = NULL,
                              departments = character()) {
  assert_writable(con)
  existing <- db_get_one(con, "SELECT actor_id FROM actors WHERE idp = ? AND external_id = ?",
                         list(idp, external_id))
  if (!is.null(existing)) {
    DBI::dbExecute(con, "UPDATE actors SET display_name = ?, email = ?, departments_json = ?
                         WHERE actor_id = ?",
                   params = list(display_name, nullable(email),
                                 as.character(to_json(as.list(departments))), existing$actor_id))
    return(existing$actor_id)
  }
  orph_create_actor(con, display_name, email, idp, external_id, departments)
}

#' Read an actor
#' @param con A connection.
#' @param actor_id Actor identifier.
#' @return A list, or `NULL`.
#' @export
orph_get_actor <- function(con, actor_id) {
  row <- db_get_one(con, "SELECT * FROM actors WHERE actor_id = ?", list(actor_id))
  if (is.null(row)) return(NULL)
  row$is_admin <- isTRUE(as.integer(row$is_admin) == 1L)
  row$departments <- unlist(from_json(row$departments_json) %||% list())
  row$disabled <- !is.na(row$disabled_at)
  row
}

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

#' @keywords internal
hash_token <- function(token) digest::digest(paste0("orpheus:", token), algo = "sha256")

#' Issue an API token for an actor
#'
#' The token is returned once and only its hash is stored, so a leaked database
#' does not hand over working credentials.
#'
#' @param con A writable connection.
#' @param actor_id Actor to issue for.
#' @param label What the token is for.
#' @param expires_at Optional ISO-8601 expiry.
#' @return A list with `token` (shown once) and `token_id`.
#' @export
orph_create_token <- function(con, actor_id, label = NULL, expires_at = NULL) {
  assert_writable(con)
  if (is.null(orph_get_actor(con, actor_id))) cli::cli_abort("No actor {.val {actor_id}}.")
  raw <- paste0(format(as.hexmode(sample.int(65536L, 16L, replace = TRUE) - 1L), width = 4L),
                collapse = "")
  token_id <- orph_id("tok")
  db_insert(con, "actor_tokens", list(
    token_id = token_id, actor_id = actor_id, token_hash = hash_token(raw),
    label = nullable(label), created_at = orph_now(), expires_at = nullable(expires_at)))
  record_edit(con, "actor_tokens", token_id, NULL, "token_issued", NULL,
              list(actor_id = actor_id, label = label), actor_id = actor_id)
  list(token_id = token_id, actor_id = actor_id, token = raw,
       note = "Store this now. Only its hash is kept.")
}

#' Revoke a token
#' @param con A writable connection.
#' @param token_id Token identifier.
#' @param actor_id Actor performing the revocation.
#' @export
orph_revoke_token <- function(con, token_id, actor_id = NULL) {
  assert_writable(con)
  DBI::dbExecute(con, "UPDATE actor_tokens SET revoked_at = ? WHERE token_id = ?",
                 params = list(orph_now(), token_id))
  record_edit(con, "actor_tokens", token_id, NULL, "token_revoked", NULL, NULL, actor_id)
  invisible(TRUE)
}

#' Resolve a bearer token to an actor
#'
#' @param con A connection.
#' @param token The bearer token.
#' @return The actor list, or `NULL` when the token is unknown, revoked,
#'   expired, or its actor is disabled.
#' @export
orph_authenticate <- function(con, token) {
  if (is.null(token) || !nzchar(token)) return(NULL)
  row <- db_get_one(con,
    "SELECT actor_id, expires_at, revoked_at FROM actor_tokens WHERE token_hash = ?",
    list(hash_token(token)))
  if (is.null(row)) return(NULL)
  if (!is.na(row$revoked_at)) return(NULL)
  if (!is.na(row$expires_at) && row$expires_at < orph_now()) return(NULL)
  actor <- orph_get_actor(con, row$actor_id)
  if (is.null(actor) || isTRUE(actor$disabled)) return(NULL)
  actor
}

# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

#' Can this actor take this action on this document?
#'
#' Resolution order, highest first: deployment administrator, document owner,
#' explicit share, then the document's visibility level. Anonymous requests are
#' refused outright -- there is no anonymous audience for contract documents.
#'
#' @param con A connection.
#' @param actor An actor list from [orph_authenticate()], or `NULL`.
#' @param document_id Document identifier.
#' @param action One of [ORPH_ACTIONS].
#' @return `TRUE` or `FALSE`.
#' @export
orph_can <- function(con, actor, document_id, action = c("view", "edit", "share", "delete")) {
  action <- match.arg(action)
  if (is.null(actor) || is.null(actor$actor_id)) return(FALSE)
  if (isTRUE(actor$is_admin)) return(TRUE)

  doc <- db_get_one(con, "SELECT created_by, visibility FROM documents WHERE document_id = ?",
                    list(document_id))
  if (is.null(doc)) return(FALSE)

  if (!is.na(doc$created_by) && identical(doc$created_by, actor$actor_id)) return(TRUE)

  # Sharing and deleting stay with the owner and administrators. A share
  # cannot be used to widen a share.
  if (action %in% c("share", "delete")) return(FALSE)

  share <- db_get_one(con, "SELECT role FROM document_shares WHERE document_id = ? AND actor_id = ?",
                      list(document_id, actor$actor_id))
  if (!is.null(share)) {
    if (action == "view") return(TRUE)
    if (action == "edit") return(identical(share$role, "editor"))
  }

  vis <- doc$visibility %||% "private"
  if (identical(vis, "link-view")) return(action == "view")
  if (identical(vis, "link-edit")) return(action %in% c("view", "edit"))
  FALSE
}

#' Assert a permission, aborting if absent
#' @param con A connection.
#' @param actor An actor list.
#' @param document_id Document identifier.
#' @param action Action to check.
#' @return Invisibly `TRUE`.
#' @export
orph_require <- function(con, actor, document_id, action) {
  if (!orph_can(con, actor, document_id, action)) {
    cli::cli_abort(c("Not permitted to {action} document {.val {document_id}}.",
                     i = "Ask its owner for access."), class = "orph_forbidden")
  }
  invisible(TRUE)
}

#' Share a document with an actor
#' @param con A writable connection.
#' @param document_id Document identifier.
#' @param actor_id Actor to share with.
#' @param role `"viewer"` or `"editor"`.
#' @param granted_by Actor granting the share; must be able to `share`.
#' @export
orph_share_document <- function(con, document_id, actor_id, role = c("viewer", "editor"),
                                granted_by) {
  assert_writable(con)
  role <- match.arg(role)
  granter <- orph_get_actor(con, granted_by)
  orph_require(con, granter, document_id, "share")
  if (is.null(orph_get_actor(con, actor_id))) cli::cli_abort("No actor {.val {actor_id}}.")

  with_tx(con, {
    DBI::dbExecute(con,
      "INSERT INTO document_shares (document_id, actor_id, role, granted_by, granted_at)
       VALUES (?, ?, ?, ?, ?)
       ON CONFLICT(document_id, actor_id) DO UPDATE SET
         role = excluded.role, granted_by = excluded.granted_by, granted_at = excluded.granted_at",
      params = list(document_id, actor_id, role, granted_by, orph_now()))
    # Permission changes are audited alongside data changes: in a public-sector
    # deployment "who could see this, and since when" is as much of a question
    # as "who changed this".
    record_edit(con, "document_shares", paste0(document_id, "/", actor_id), document_id,
                "share_granted", NULL, list(actor_id = actor_id, role = role),
                actor_id = granted_by)
  })
  invisible(TRUE)
}

#' Revoke a document share
#' @param con A writable connection.
#' @param document_id Document identifier.
#' @param actor_id Actor whose share is revoked.
#' @param revoked_by Actor performing the revocation.
#' @export
orph_unshare_document <- function(con, document_id, actor_id, revoked_by) {
  assert_writable(con)
  orph_require(con, orph_get_actor(con, revoked_by), document_id, "share")
  with_tx(con, {
    DBI::dbExecute(con, "DELETE FROM document_shares WHERE document_id = ? AND actor_id = ?",
                   params = list(document_id, actor_id))
    record_edit(con, "document_shares", paste0(document_id, "/", actor_id), document_id,
                "share_revoked", list(actor_id = actor_id), NULL, actor_id = revoked_by)
  })
  invisible(TRUE)
}

#' Set a document's visibility
#' @param con A writable connection.
#' @param document_id Document identifier.
#' @param visibility One of [ORPH_VISIBILITY].
#' @param actor_id Actor making the change; must be able to `share`.
#' @export
orph_set_visibility <- function(con, document_id, visibility, actor_id) {
  assert_writable(con)
  assert_choice(visibility, ORPH_VISIBILITY, "visibility")
  orph_require(con, orph_get_actor(con, actor_id), document_id, "share")
  before <- db_get_one(con, "SELECT visibility FROM documents WHERE document_id = ?",
                       list(document_id))
  with_tx(con, {
    DBI::dbExecute(con, "UPDATE documents SET visibility = ? WHERE document_id = ?",
                   params = list(visibility, document_id))
    record_edit(con, "documents", document_id, document_id, "visibility_changed",
                list(visibility = before$visibility), list(visibility = visibility), actor_id)
  })
  invisible(TRUE)
}

#' Documents an actor may view
#' @param con A connection.
#' @param actor An actor list.
#' @param limit Maximum rows.
#' @return A data frame.
#' @export
orph_visible_documents <- function(con, actor, limit = 100L) {
  if (is.null(actor)) return(orph_list_documents(con, 0L))
  if (isTRUE(actor$is_admin)) return(orph_list_documents(con, limit))
  db_query(con,
    "SELECT d.document_id, d.filename, d.doc_type, d.sector, d.jurisdiction,
            d.review_status, d.visibility, d.n_pages, d.date_added, d.created_by
     FROM documents d
     WHERE d.created_by = ?
        OR d.visibility IN ('link-view', 'link-edit')
        OR EXISTS (SELECT 1 FROM document_shares s
                   WHERE s.document_id = d.document_id AND s.actor_id = ?)
     ORDER BY d.date_added DESC LIMIT ?",
    list(actor$actor_id, actor$actor_id, as.integer(limit)))
}

#' The permission rule as SQL, for Datasette
#'
#' Datasette resolves per-resource permissions by running SQL that returns the
#' resources an actor may reach. Emitting the same rule from one place is what
#' keeps a user's access identical whether they come through the API or browse
#' Datasette directly -- two hand-written copies of this rule would drift, and
#' the drift would be a silent permission bug.
#'
#' Bind `:actor_id` to the authenticated actor.
#'
#' @param action `"view"` or `"edit"`.
#' @return A SQL string.
#' @export
orph_permission_sql <- function(action = c("view", "edit")) {
  action <- match.arg(action)
  visibility <- if (action == "view") "('link-view', 'link-edit')" else "('link-edit')"
  share_clause <- if (action == "view") "" else " AND s.role = 'editor'"
  sprintf(
"-- Documents an actor may %s. Bind :actor_id.
SELECT d.document_id AS resource
FROM documents d
LEFT JOIN actors a ON a.actor_id = :actor_id
WHERE a.actor_id IS NOT NULL
  AND (
    a.is_admin = 1
    OR d.created_by = :actor_id
    OR d.visibility IN %s
    OR EXISTS (
      SELECT 1 FROM document_shares s
      WHERE s.document_id = d.document_id AND s.actor_id = :actor_id%s
    )
  )", action, visibility, share_clause)
}
