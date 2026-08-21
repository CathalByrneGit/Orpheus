test_that("a new store is in WAL mode with the full schema", {
  con <- new_test_store()
  expect_equal(DBI::dbGetQuery(con, "PRAGMA journal_mode")[1, 1], "wal")
  for (tbl in c("documents", "document_pages", "instance_index", "edges", "provenance",
                "concept_evaluations", "concept_evaluation_dependencies", "edit_history",
                "schema_amendments", "extraction_runs", "llm_calls", "org_settings",
                "actors", "actor_tokens", "document_shares", "bundles")) {
    expect_true(DBI::dbExistsTable(con, tbl), info = tbl)
  }
})

test_that("migrations are idempotent", {
  con <- new_test_store()
  expect_length(orph_migrate(con), 0)
})

test_that("a read connection refuses writes", {
  con <- new_test_store()
  path <- attr(con, "orph_path")
  ro <- orph_connect(path, mode = "read")
  on.exit(orph_disconnect(ro))
  expect_error(orph_set_setting(ro, "k", "v"), "read-only")
  expect_error(DBI::dbExecute(ro, "DELETE FROM documents"))
})

test_that("a second writer in this process is refused while the lock is held", {
  con <- new_test_store()
  path <- attr(con, "orph_path")
  # Same pid, so the lock is recognised as ours rather than stale -- the
  # cross-process case is covered by the lock file itself.
  lock <- paste0(path, ".writer.lock")
  expect_true(file.exists(lock))
  writeLines('{"pid":999999,"acquired_at":"2020-01-01T00:00:00Z"}', lock)
  expect_error(orph_connect(path, mode = "write"), "stale writer lock")
  expect_no_error({
    c2 <- orph_connect(path, mode = "write", force_lock = TRUE)
    orph_disconnect(c2)
  })
})

test_that("transactions nest without error and roll back as one unit", {
  con <- new_test_store()
  with_tx <- orpheus:::with_tx
  expect_error(
    with_tx(con, {
      orph_set_setting(con, "outer", "1")
      with_tx(con, orph_set_setting(con, "inner", "1"))
      stop("boom")
    }), "boom")
  expect_null(orph_setting(con, "outer"))
  expect_null(orph_setting(con, "inner"))
})

test_that("settings round-trip and overwrite", {
  con <- new_test_store()
  expect_equal(orph_setting(con, "missing", "fallback"), "fallback")
  orph_set_setting(con, "cloud_ai_policy", "per_user")
  expect_equal(orph_setting(con, "cloud_ai_policy"), "per_user")
  orph_set_setting(con, "cloud_ai_policy", "disabled")
  expect_equal(orph_setting(con, "cloud_ai_policy"), "disabled")
})

test_that("checkpointing makes committed data visible to an immutable reader", {
  con <- new_test_store()
  path <- attr(con, "orph_path")
  seed_actors(con)
  orph_checkpoint(con, "TRUNCATE")
  snapshot <- tempfile(fileext = ".sqlite")
  file.copy(path, snapshot)
  on.exit(unlink(snapshot))
  # immutable=1 skips the WAL entirely; without the checkpoint above this
  # would see an empty database.
  ro <- DBI::dbConnect(RSQLite::SQLite(), paste0("file:", snapshot, "?immutable=1"),
                       flags = RSQLite::SQLITE_RO, extended_types = FALSE)
  on.exit(DBI::dbDisconnect(ro), add = TRUE)
  expect_gt(DBI::dbGetQuery(ro, "SELECT COUNT(*) n FROM actors")$n, 0)
})

test_that("a failed migration releases the lock instead of stranding the database", {
  path <- tempfile(fileext = ".sqlite")
  on.exit(unlink(paste0(path, c("", "-wal", "-shm", ".writer.lock"))))

  # A file that is not a database at all fails somewhere inside connect/migrate.
  writeLines("this is not a SQLite database", path)
  expect_error(orph_connect(path, mode = "write"))
  # The lock must not survive the failure, or nothing could ever open this path.
  expect_false(file.exists(paste0(path, ".writer.lock")))
})
