test_that("extraction writes instances, edges, provenance and an audit row", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)

  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM instances_Contract")$n, 1)
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM edges")$n, 1)

  contract <- DBI::dbGetQuery(con, "SELECT * FROM instances_Contract")
  expect_equal(contract$source, "ai_local")
  expect_equal(contract$status, "unconfirmed")
  expect_equal(contract$confidence, 0.9)
  expect_equal(contract$document_id, doc)

  prov <- DBI::dbGetQuery(con, "SELECT * FROM provenance WHERE instance_id = ?",
                          params = list(contract$instance_id))
  expect_equal(nrow(prov), 1)
  expect_match(prov$excerpt, "SERVICES AGREEMENT")

  expect_gt(nrow(orph_row_history(con, "instances_Contract", contract$instance_id)), 0)
})

test_that("the deterministic pass runs before the model and links to the contract", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)

  dates <- DBI::dbGetQuery(con, "SELECT * FROM instances_KeyDate ORDER BY value")
  expect_gte(nrow(dates), 2)
  expect_true(all(!is.na(dates$contract_instance_id)))
  expect_equal(dates$date_role[dates$value == "2024-01-01"], "start")
  expect_equal(dates$date_role[dates$value == "2026-12-31"], "end")

  amounts <- DBI::dbGetQuery(con, "SELECT * FROM instances_MonetaryAmount")
  expect_equal(amounts$amount, 2400000)
  expect_equal(amounts$currency, "EUR")
})

test_that("confidence from the engine is snapped to the rubric on the way in", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = function(bundle, source, llm_fn, tier) list(
    entities = list(list(instance_id = "e1", type_id = "Company", confidence = 0.83,
                         source_refs = list(list(source_label = "x", excerpt = "Acme")),
                         properties = list(name = "Acme"))),
    relationships = list(), amendments = list()))
  seed_document(con, root)
  expect_equal(DBI::dbGetQuery(con, "SELECT confidence FROM instances_Company")$confidence, 0.7)
})

test_that("naive_key is derived at write time, never taken from the model", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = function(bundle, source, llm_fn, tier) list(
    entities = list(list(instance_id = "e1", type_id = "Company", confidence = 0.9,
                         source_refs = list(list(source_label = "x", excerpt = "y")),
                         # The model supplies a wrong key; it must be ignored.
                         properties = list(name = "Meridian Systems Ltd.",
                                           naive_key = "SOMETHING WRONG"))),
    relationships = list(), amendments = list()))
  seed_document(con, root)
  expect_equal(DBI::dbGetQuery(con, "SELECT naive_key FROM instances_Company")$naive_key,
               "meridian systems")
})

test_that("an undeclared property becomes a schema amendment instead of being dropped", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = fake_populator(extra_properties = list(renewal_notice_period = "90 days")))
  seed_document(con, root)

  queue <- orph_schema_amendments(con)
  row <- queue[queue$amendment_type == "new_property", ]
  expect_equal(nrow(row), 1)
  expect_equal(row$type_id, "Contract")
  expect_equal(row$property_id, "renewal_notice_period")
  expect_equal(row$observed_value, "90 days")
})

test_that("an undeclared object type becomes a schema amendment and writes no row", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = fake_populator(extra_entities = list(
    list(instance_id = "e9", type_id = "Vessel", confidence = 0.5,
         source_refs = list(list(source_label = "x", excerpt = "y")),
         properties = list(name = "MV Something")))))
  seed_document(con, root)

  queue <- orph_schema_amendments(con)
  expect_true("new_type" %in% queue$amendment_type)
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM instance_index WHERE type_id = 'Vessel'")$n, 0)
})

test_that("repeat sightings of an amendment increment a counter, not the queue length", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = fake_populator(extra_properties = list(renewal_notice_period = "90 days")))
  seed_document(con, root, name = "a.txt")
  seed_document(con, root, name = "b.txt", value = "EUR 999,000")

  queue <- orph_schema_amendments(con)
  row <- queue[queue$property_id == "renewal_notice_period", ]
  expect_equal(nrow(row), 1)
  expect_equal(row$occurrences, 2)
})

test_that("an edge naming an instance the engine did not return is dropped, not stored", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = fake_populator(relationships = list(
    list(link_type_id = "party_to", from_instance_id = "e2", to_instance_id = "e1",
         confidence = 0.9, evidence = "real"),
    list(link_type_id = "party_to", from_instance_id = "ghost", to_instance_id = "e1",
         confidence = 0.5, evidence = "dangling"))))
  doc <- seed_document(con, root)
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM edges")$n, 1)
})

test_that("an unknown link type is queued rather than written as an edge", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = fake_populator(relationships = list(
    list(link_type_id = "invented_link", from_instance_id = "e2", to_instance_id = "e1",
         confidence = 0.9, evidence = "x"))))
  seed_document(con, root)
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM edges")$n, 0)
  expect_true("new_link_type" %in% orph_schema_amendments(con)$amendment_type)
})

test_that("a failed extraction is recorded as failed rather than left running", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = function(...) stop("engine exploded"))
  path <- write_contract_file()
  ing <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)
  expect_error(orph_extract(con, ing$document_id, "local", actor_id = "act_test"),
               "Extraction failed")
  run <- DBI::dbGetQuery(con, "SELECT status, error FROM extraction_runs")
  expect_equal(run$status, "failed")
  expect_match(run$error, "engine exploded")
})

test_that("extraction refuses a document with no text", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  blank <- file.path(tempdir(), paste0(as.integer(runif(1, 1, 1e9)), "-blank.txt"))
  writeLines("", blank)
  ing <- orph_ingest(con, blank, actor_id = "act_test", storage_root = root)
  expect_error(orph_extract(con, ing$document_id, "local", actor_id = "act_test"),
               "no text")
})

test_that("re-running a tier is refused rather than silently duplicating everything", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id
  orph_extract(con, doc, "local", actor_id = "act_test")

  expect_error(orph_extract(con, doc, "local", actor_id = "act_test"), "already run")
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM instances_Contract")$n, 1)
})

test_that("a forced re-run supersedes unreviewed rows but keeps human decisions", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id
  orph_extract(con, doc, "local", actor_id = "act_test")

  confirmed <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Company")$instance_id
  orph_confirm_instance(con, confirmed, "act_test")

  orph_extract(con, doc, "local", actor_id = "act_test", force = TRUE)

  contracts <- DBI::dbGetQuery(con, "SELECT status FROM instances_Contract ORDER BY created_at")
  expect_equal(contracts$status, c("rejected", "unconfirmed"))

  companies <- DBI::dbGetQuery(con, "SELECT status FROM instances_Company ORDER BY created_at")
  # The confirmed row survives untouched; only the fresh one is added.
  expect_true("confirmed" %in% companies$status)
  expect_false("rejected" %in% companies$status)

  # And the supersession is in the audit trail, not silent.
  expect_true("superseded" %in% orph_document_history(con, doc)$action)
})

test_that("the two tiers do not block each other", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  orph_set_setting(con, "cloud_ai_policy", "org_allow")
  old <- orph_set_llm_provider("cloud", fake_llm("{}")); on.exit(orph_set_llm_provider("cloud", old))

  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id
  orph_extract(con, doc, "local", actor_id = "act_test")
  expect_no_error(orph_extract(con, doc, "cloud", actor_id = "act_test", opt_in = TRUE))

  sources <- DBI::dbGetQuery(con, "SELECT DISTINCT source FROM instances_Contract")$source
  expect_setequal(sources, c("ai_local", "ai_cloud"))
})

test_that("retrying after a failed extraction does not duplicate deterministic findings", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id

  # The deterministic pass commits before the model pass, so its findings
  # survive a model failure -- which is wanted. What is not wanted is a second
  # copy of them when the user fixes the problem and runs again.
  use_fakes(populator = function(...) stop("no engine available"))
  expect_error(orph_extract(con, doc, "local", actor_id = "act_test"), "Extraction failed")
  after_failure <- DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM instances_KeyDate")$n
  expect_gt(after_failure, 0)

  # The run is recorded failed, so a retry is allowed without force = TRUE.
  expect_equal(DBI::dbGetQuery(con, "SELECT status FROM extraction_runs")$status, "failed")

  orph_set_populator(fake_populator())
  orph_extract(con, doc, "local", actor_id = "act_test")

  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM instances_KeyDate")$n, after_failure)
  expect_equal(
    DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM (SELECT raw_text, page_no FROM instances_KeyDate
                          GROUP BY raw_text, page_no HAVING COUNT(*) > 1)")$n, 0)
})

test_that("a forced re-run still refreshes deterministic findings", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id
  orph_extract(con, doc, "local", actor_id = "act_test")
  first <- DBI::dbGetQuery(con,
    "SELECT COUNT(*) n FROM instances_KeyDate WHERE status = 'unconfirmed'")$n
  expect_gt(first, 0)

  # force supersedes the old findings, so fresh ones are written rather than
  # being suppressed by the duplicate guard.
  orph_extract(con, doc, "local", actor_id = "act_test", force = TRUE)
  expect_equal(DBI::dbGetQuery(con,
    "SELECT COUNT(*) n FROM instances_KeyDate WHERE status = 'unconfirmed'")$n, first)
  expect_equal(DBI::dbGetQuery(con,
    "SELECT COUNT(*) n FROM instances_KeyDate WHERE status = 'rejected'")$n, first)
})
