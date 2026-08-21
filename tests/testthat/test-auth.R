test_that("a token round-trips and only its hash is stored", {
  con <- new_test_store(); actors <- seed_actors(con)
  issued <- orph_create_token(con, actors$owner, "cli")

  expect_equal(orph_authenticate(con, issued$token)$actor_id, actors$owner)
  stored <- DBI::dbGetQuery(con, "SELECT token_hash FROM actor_tokens")$token_hash
  expect_false(any(grepl(issued$token, stored, fixed = TRUE)))
  expect_equal(nchar(stored), 64)
})

test_that("unknown, revoked and expired tokens all resolve to no actor", {
  con <- new_test_store(); actors <- seed_actors(con)
  expect_null(orph_authenticate(con, "not-a-token"))
  expect_null(orph_authenticate(con, ""))
  expect_null(orph_authenticate(con, NULL))

  revoked <- orph_create_token(con, actors$owner, "revoke me")
  orph_revoke_token(con, revoked$token_id, actors$owner)
  expect_null(orph_authenticate(con, revoked$token))

  expired <- orph_create_token(con, actors$owner, "expired", expires_at = "2000-01-01T00:00:00Z")
  expect_null(orph_authenticate(con, expired$token))
})

test_that("a disabled actor cannot authenticate with a valid token", {
  con <- new_test_store(); actors <- seed_actors(con)
  issued <- orph_create_token(con, actors$owner, "cli")
  DBI::dbExecute(con, "UPDATE actors SET disabled_at = ? WHERE actor_id = ?",
                 params = list("2020-01-01T00:00:00Z", actors$owner))
  expect_null(orph_authenticate(con, issued$token))
})

test_that("an external identity maps to one actor across logins", {
  con <- new_test_store()
  first  <- orph_upsert_actor(con, "entra", "abc-123", "Nuala Ryan", "n@x.ie", c("health"))
  second <- orph_upsert_actor(con, "entra", "abc-123", "Nuala Ryan-Smith", "n@x.ie", c("health", "ict"))
  expect_equal(first, second)
  actor <- orph_get_actor(con, first)
  expect_equal(actor$display_name, "Nuala Ryan-Smith")
  expect_setequal(actor$departments, c("health", "ict"))
})

describe_permissions <- function(con, actor_id, doc) {
  actor <- orph_get_actor(con, actor_id)
  vapply(c("view", "edit", "share", "delete"),
         function(a) orph_can(con, actor, doc, a), logical(1))
}

test_that("owner, admin and stranger get the access the model says they should", {
  con <- new_test_store(); root <- test_storage_root(); actors <- seed_actors(con)
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = actors$owner, storage_root = root)$document_id

  expect_true(all(describe_permissions(con, actors$owner, doc)))
  expect_true(all(describe_permissions(con, actors$admin, doc)))
  expect_false(any(describe_permissions(con, actors$other, doc)))
  expect_false(orph_can(con, NULL, doc, "view"))
})

test_that("a share grants exactly its role and cannot be used to widen itself", {
  con <- new_test_store(); root <- test_storage_root(); actors <- seed_actors(con)
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = actors$owner, storage_root = root)$document_id

  orph_share_document(con, doc, actors$other, "viewer", granted_by = actors$owner)
  expect_equal(unname(describe_permissions(con, actors$other, doc)),
               c(TRUE, FALSE, FALSE, FALSE))

  orph_share_document(con, doc, actors$other, "editor", granted_by = actors$owner)
  expect_equal(unname(describe_permissions(con, actors$other, doc)),
               c(TRUE, TRUE, FALSE, FALSE))

  # An editor cannot re-share: sharing stays with the owner and administrators.
  third <- orph_create_actor(con, "Third")
  expect_error(orph_share_document(con, doc, third, "viewer", granted_by = actors$other),
               "Not permitted")

  orph_unshare_document(con, doc, actors$other, revoked_by = actors$owner)
  expect_false(orph_can(con, orph_get_actor(con, actors$other), doc, "view"))
})

test_that("visibility levels behave as datasette-paper's model describes", {
  con <- new_test_store(); root <- test_storage_root(); actors <- seed_actors(con)
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = actors$owner, storage_root = root)$document_id

  orph_set_visibility(con, doc, "link-view", actor_id = actors$owner)
  expect_equal(unname(describe_permissions(con, actors$other, doc)),
               c(TRUE, FALSE, FALSE, FALSE))

  orph_set_visibility(con, doc, "link-edit", actor_id = actors$owner)
  expect_equal(unname(describe_permissions(con, actors$other, doc)),
               c(TRUE, TRUE, FALSE, FALSE))

  orph_set_visibility(con, doc, "private", actor_id = actors$owner)
  expect_false(any(describe_permissions(con, actors$other, doc)))
  # Still no anonymous audience at any visibility level.
  expect_false(orph_can(con, NULL, doc, "view"))
})

test_that("permission changes are audited", {
  con <- new_test_store(); root <- test_storage_root(); actors <- seed_actors(con)
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = actors$owner, storage_root = root)$document_id
  orph_share_document(con, doc, actors$other, "viewer", granted_by = actors$owner)
  orph_set_visibility(con, doc, "link-view", actor_id = actors$owner)

  actions <- orph_document_history(con, doc)$action
  expect_true("share_granted" %in% actions)
  expect_true("visibility_changed" %in% actions)
})

test_that("the document list shows only what the actor may see", {
  con <- new_test_store(); root <- test_storage_root(); actors <- seed_actors(con)
  mine   <- orph_ingest(con, write_contract_file("a.txt"), actor_id = actors$owner,
                        storage_root = root)$document_id
  theirs <- orph_ingest(con, write_contract_file("b.txt", supplier = "Other Ltd"),
                        actor_id = actors$other, storage_root = root)$document_id

  expect_equal(orph_visible_documents(con, orph_get_actor(con, actors$owner))$document_id, mine)
  expect_equal(orph_visible_documents(con, orph_get_actor(con, actors$other))$document_id, theirs)
  expect_equal(nrow(orph_visible_documents(con, orph_get_actor(con, actors$admin))), 2)
  expect_equal(nrow(orph_visible_documents(con, NULL)), 0)
})

test_that("the Datasette permission SQL agrees with orph_can for every actor", {
  con <- new_test_store(); root <- test_storage_root(); actors <- seed_actors(con)
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = actors$owner, storage_root = root)$document_id
  orph_share_document(con, doc, actors$other, "viewer", granted_by = actors$owner)

  for (action in c("view", "edit")) {
    sql <- gsub(":actor_id", "?", orph_permission_sql(action), fixed = TRUE)
    for (id in unlist(actors)) {
      from_sql <- nrow(DBI::dbGetQuery(con, sql, params = list(id, id, id))) > 0
      from_r   <- orph_can(con, orph_get_actor(con, id), doc, action)
      expect_equal(from_sql, from_r,
                   info = paste("actor", id, "action", action))
    }
  }
})
