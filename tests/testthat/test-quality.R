# Extraction quality is the Phase 1 question, so these tests pin down the two
# ways the measurement could quietly lie: counting unreviewed rows as correct,
# and attributing a corrected fact to the confidence it has *after* correction.

seed_reviewed_corpus <- function(con, root, n = 6) {
  for (i in seq_len(n)) {
    orph_set_populator(function(bundle, source, llm_fn, tier) list(
      entities = list(
        list(instance_id = "a", type_id = "Contract", confidence = 0.9,
             source_refs = list(list(source_label = "d", excerpt = "AGREEMENT")),
             properties = list(name = paste("Agreement", i), value_amount = 1000000,
                               value_currency = "EUR", signature_block_present = "no")),
        list(instance_id = "b", type_id = "Person", confidence = 0.5,
             source_refs = list(list(source_label = "d", excerpt = "signatory")),
             properties = list(name = paste("Person", i), job_title = "Officer"))),
      relationships = list(), amendments = list()))
    path <- write_contract_file(name = paste0("doc", i, ".txt"))
    doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id
    orph_extract(con, doc, "local", actor_id = "act_test")
  }
}

test_that("an unreviewed store reports unmeasured rather than good", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  seed_document(con, root)

  report <- orph_quality_report(con)
  expect_equal(report$readiness$state, "unmeasured")
  expect_match(report$readiness$note, "not good, not bad")
  expect_true(is.na(report$overall$accuracy) || report$overall$n_reviewed == 0)
})

test_that("thin review is reported as insufficient, not as a score", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  seed_reviewed_corpus(con, root, n = 6)

  one <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract LIMIT 1")$instance_id
  orph_confirm_instance(con, one, "act_test")

  report <- orph_quality_report(con)
  expect_equal(report$readiness$state, "insufficient_review")
  expect_match(report$readiness$note, "Too little to judge")
})

test_that("rates count reviewed rows only", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  seed_reviewed_corpus(con, root, n = 6)

  ids <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract")$instance_id
  for (i in seq_along(ids)) {
    if (i <= 3) orph_confirm_instance(con, ids[i], "act_test")
    else if (i <= 4) orph_reject_instance(con, ids[i], "act_test")
  }

  q <- orph_extraction_quality(con, min_reviewed = 1L)
  contract <- q$by_type[q$by_type$type_id == "Contract", ]
  expect_equal(contract$n_total, 6)
  expect_equal(contract$n_reviewed, 4)
  expect_equal(contract$coverage, round(4 / 6, 3))
  # 3 of 4 reviewed, not 3 of 6: the two untouched rows are unknown, not correct.
  expect_equal(contract$accuracy, 0.75)
})

test_that("an amended row is scored against the confidence the machine gave it", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  seed_reviewed_corpus(con, root, n = 6)

  # Amending sets the row's confidence to 1.0 and source to human, because it
  # is ground truth afterwards. The measurement must still attribute it to the
  # 0.9 the model originally claimed, or every correction would be reported as
  # a full-confidence success.
  ids <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract")$instance_id
  for (id in ids) orph_amend_instance(con, id, list(name = "Corrected"), "act_test")

  expect_equal(unique(DBI::dbGetQuery(con, "SELECT confidence FROM instances_Contract")$confidence), 1.0)

  q <- orph_extraction_quality(con, min_reviewed = 1L)
  explicit <- q$by_confidence[q$by_confidence$confidence == 1.0, ]
  named    <- q$by_confidence[q$by_confidence$confidence == 0.9, ]

  expect_true(nrow(explicit) == 0 || explicit$n_reviewed == 0)
  expect_equal(named$n_reviewed, 6)
  expect_equal(named$accuracy, 0)      # all six needed correction
  expect_equal(named$amend_rate, 1)
})

test_that("the tier is attributed from provenance, not from the amended row", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  seed_reviewed_corpus(con, root, n = 6)
  ids <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Person")$instance_id
  for (id in ids) orph_amend_instance(con, id, list(job_title = "Director"), "act_test")

  q <- orph_extraction_quality(con, min_reviewed = 1L)
  expect_true("ai_local" %in% q$by_tier$source)
  expect_false("human" %in% q$by_tier$source)
})

test_that("calibration detects a rubric that does not rank reliability", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  seed_reviewed_corpus(con, root, n = 6)

  # Confirm every low-confidence row and reject every high-confidence one:
  # the rubric is then exactly backwards and must be reported as such.
  for (id in DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Person")$instance_id) {
    orph_confirm_instance(con, id, "act_test")
  }
  for (id in DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract")$instance_id) {
    orph_reject_instance(con, id, "act_test")
  }

  cal <- orph_confidence_calibration(con, min_reviewed = 1L)
  expect_equal(cal$verdict, "inverted")
  expect_gt(nrow(cal$inversions), 0)
  expect_match(cal$note, "not ranking reliability")
})

test_that("calibration refuses to judge on too little evidence", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  seed_reviewed_corpus(con, root, n = 6)
  one <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract LIMIT 1")$instance_id
  orph_confirm_instance(con, one, "act_test")

  cal <- orph_confidence_calibration(con, min_reviewed = 5L)
  expect_equal(cal$verdict, "insufficient_evidence")
})

test_that("rule flags are measured as rule precision, not as extraction quality", {
  skip_if_no_conceptr()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  seed_reviewed_corpus(con, root, n = 6)
  orph_setup_concepts(con, actor_id = "act_admin")
  for (d in DBI::dbGetQuery(con, "SELECT document_id FROM documents")$document_id) {
    orph_evaluate_concepts(con, d, actor_id = "act_test")
  }

  flags <- DBI::dbGetQuery(con,
    "SELECT instance_id FROM instances_Flag WHERE flag_type = 'missing_signature'")$instance_id
  expect_gt(length(flags), 0)
  for (i in seq_along(flags)) {
    if (i <= 2) orph_confirm_instance(con, flags[i], "act_test")
    else orph_reject_instance(con, flags[i], "act_test")
  }

  # Rule flags must not appear in the extraction figures at all.
  q <- orph_extraction_quality(con, min_reviewed = 1L)
  expect_false("Flag" %in% q$by_type$type_id)

  # They are measured here instead.
  precision <- orph_concept_precision(con, min_reviewed = 1L)
  row <- precision[precision$concept_id == "missing_signature", ]
  expect_equal(row$n_reviewed, length(flags))
  expect_equal(row$n_upheld, 2)
  expect_equal(row$precision, round(2 / length(flags), 3))
})

test_that("a rate is withheld when too few rows back it", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  seed_reviewed_corpus(con, root, n = 6)
  one <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract LIMIT 1")$instance_id
  orph_confirm_instance(con, one, "act_test")

  strict <- orph_extraction_quality(con, min_reviewed = 5L)
  contract <- strict$by_type[strict$by_type$type_id == "Contract", ]
  expect_equal(contract$n_reviewed, 1)
  expect_true(is.na(contract$accuracy))

  loose <- orph_extraction_quality(con, min_reviewed = 1L)
  expect_equal(loose$by_type[loose$by_type$type_id == "Contract", ]$accuracy, 1)
})

test_that("corrected properties are ranked with a worked example", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  seed_reviewed_corpus(con, root, n = 6)

  for (id in DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract")$instance_id) {
    orph_amend_instance(con, id, list(name = "Corrected name"), "act_test")
  }
  one <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Person LIMIT 1")$instance_id
  orph_amend_instance(con, one, list(job_title = "Director"), "act_test")

  corrections <- orph_property_corrections(con)
  expect_equal(corrections$property[[1]], "name")          # most corrected, ranked first
  expect_equal(corrections$n_corrections[[1]], 6)
  expect_match(corrections$example_was[[1]], "Agreement")
  expect_equal(corrections$example_became[[1]], "Corrected name")
  expect_true("job_title" %in% corrections$property)
})

test_that("the report can be scoped to one document", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  seed_reviewed_corpus(con, root, n = 6)
  docs <- DBI::dbGetQuery(con, "SELECT document_id FROM documents ORDER BY date_added")$document_id

  ids <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract WHERE document_id = ?",
                         params = list(docs[[1]]))$instance_id
  orph_confirm_instance(con, ids[[1]], "act_test")

  # The count is derived rather than hardcoded: the deterministic pass also
  # writes KeyDate and MonetaryAmount rows from the fixture text, so the number
  # per document is a property of the fixture, not a constant worth asserting.
  per_doc <- DBI::dbGetQuery(con,
    "SELECT COUNT(*) n FROM instance_index WHERE document_id = ? AND type_id != 'Flag'",
    params = list(docs[[1]]))$n

  scoped <- orph_quality_report(con, document_id = docs[[1]])
  expect_equal(scoped$scope, docs[[1]])
  expect_equal(scoped$overall$n_total, per_doc)
  expect_equal(scoped$overall$n_reviewed, 1)

  corpus <- orph_quality_report(con)
  expect_equal(corpus$overall$n_total, per_doc * length(docs))
  expect_gt(corpus$overall$n_total, scoped$overall$n_total)
})
