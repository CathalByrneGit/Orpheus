seed_corpus <- function(con, root, specs) {
  vapply(specs, function(spec) {
    old <- orph_set_populator(fake_populator(contract_name = spec$name,
                                             supplier = spec$supplier,
                                             value = spec$value))
    on.exit(orph_set_populator(old))
    path <- write_contract_file(name = paste0(spec$name, ".txt"), supplier = spec$supplier)
    ing <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)
    orph_extract(con, ing$document_id, "local", actor_id = "act_test")
    ing$document_id
  }, character(1))
}

test_that("the escalation refuses to run on a single-document store", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)
  expect_error(orph_corpus_analysis(con, doc, actor_id = "act_test"),
               "needs more than one document")
})

test_that("a counterparty is matched across documents despite spelling differences", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  docs <- seed_corpus(con, root, list(
    list(name = "ICT Services",    supplier = "Meridian Systems Limited", value = 2400000),
    list(name = "Data Migration",  supplier = "Meridian Systems Ltd",     value = 900000),
    list(name = "Network Support", supplier = "Meridian Systems Limited", value = 450000)))

  result <- orph_corpus_analysis(con, docs[[1]], actor_id = "act_test")
  expect_equal(result$matched_companies, 1)

  match <- result$counterparties[[1]]
  expect_equal(match$appears_in_documents, 2)
  # The Ltd/Limited difference is bridged, and the fact that it was bridged is
  # reported rather than hidden.
  expect_true(match$spelling_varies)
  expect_setequal(match$name_variants, c("Meridian Systems Limited", "Meridian Systems Ltd"))
})

test_that("results are labelled unresolved and carry the caveat", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  docs <- seed_corpus(con, root, list(
    list(name = "A", supplier = "Meridian Systems Limited", value = 2400000),
    list(name = "B", supplier = "Meridian Systems Limited", value = 900000)))

  result <- orph_corpus_analysis(con, docs[[1]], actor_id = "act_test")
  expect_equal(result$resolution_quality, "naive_unresolved")
  expect_match(result$caveat, "not resolved entities")

  stored <- DBI::dbGetQuery(con, "SELECT * FROM concept_evaluations WHERE kind = 'corpus'")
  expect_equal(stored$scope, "database")
  expect_equal(stored$resolution_quality, "naive_unresolved")
  expect_false(is.na(stored$corpus_context_used))
})

test_that("contract values are compared within one currency and summarised", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  docs <- seed_corpus(con, root, list(
    list(name = "A", supplier = "Meridian Systems Limited", value = 2400000),
    list(name = "B", supplier = "Meridian Systems Limited", value = 900000),
    list(name = "C", supplier = "Meridian Systems Limited", value = 450000)))

  comparison <- orph_corpus_analysis(con, docs[[1]], actor_id = "act_test")$value_comparison
  expect_true(comparison$available)
  expect_equal(comparison$this_value, 2400000)
  expect_equal(comparison$peer_count, 2)
  expect_equal(comparison$peer_median, 675000)
  expect_equal(comparison$ratio_to_median, 3.56)
  expect_false(comparison$mixed_currencies_excluded)
})

test_that("no shared counterparty means no comparison rather than a wrong one", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  docs <- seed_corpus(con, root, list(
    list(name = "A", supplier = "Meridian Systems Limited", value = 2400000),
    list(name = "B", supplier = "Entirely Different Co",   value = 900000)))

  result <- orph_corpus_analysis(con, docs[[1]], actor_id = "act_test")
  expect_equal(result$matched_companies, 0)
  expect_false(result$value_comparison$available)
  expect_match(result$value_comparison$reason, "No other documents share a counterparty")
})

test_that("a rejected counterparty drops out of the corpus match", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  docs <- seed_corpus(con, root, list(
    list(name = "A", supplier = "Meridian Systems Limited", value = 2400000),
    list(name = "B", supplier = "Meridian Systems Limited", value = 900000)))

  other <- DBI::dbGetQuery(con,
    "SELECT instance_id FROM instances_Company WHERE document_id = ?",
    params = list(docs[[2]]))$instance_id
  orph_reject_instance(con, other, "act_test", note = "Misread from a letterhead")

  result <- orph_corpus_analysis(con, docs[[1]], actor_id = "act_test")
  expect_equal(result$matched_companies, 0)
})

test_that("the engine used is reported so results can be traced to their query path", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  docs <- seed_corpus(con, root, list(
    list(name = "A", supplier = "Meridian Systems Limited", value = 2400000),
    list(name = "B", supplier = "Meridian Systems Limited", value = 900000)))
  result <- orph_corpus_analysis(con, docs[[1]], actor_id = "act_test")
  expect_true(result$engine %in% c("objectSetsR", "sql_fallback"))
})
