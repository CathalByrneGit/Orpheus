test_that("seed concepts register with conceptR and evaluate over extracted rows", {
  skip_if_no_conceptr()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = fake_populator(extra_properties = list(procurement_procedure = "direct")))
  doc <- seed_document(con, root)

  registered <- orph_setup_concepts(con, actor_id = "act_admin")
  expect_true(all(registered$action == "created"))
  expect_true("high_value" %in% registered$concept_id)

  results <- orph_evaluate_concepts(con, doc, actor_id = "act_test")
  expect_true(all(c("high_value", "missing_signature", "direct_award") %in% results$concept_id))
  expect_equal(results$n_true[results$concept_id == "high_value"], 1)
  # auto_renewal is a Clause concept and this clause has no renewal language.
  expect_equal(results$n_true[results$concept_id == "auto_renewal"], 0)
})

test_that("a true concept raises a Flag alongside model-raised flags", {
  skip_if_no_conceptr()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)
  orph_setup_concepts(con, actor_id = "act_admin")
  orph_evaluate_concepts(con, doc, actor_id = "act_test")

  flags <- DBI::dbGetQuery(con, "SELECT * FROM instances_Flag")
  expect_true("missing_signature" %in% flags$flag_type)
  expect_true(all(flags$raised_by_pass == "concept"))
  expect_true(all(flags$status == "unconfirmed"))
  expect_true(all(!is.na(flags$target_instance_id)))
})

test_that("re-evaluating does not duplicate flags", {
  skip_if_no_conceptr()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)
  orph_setup_concepts(con, actor_id = "act_admin")
  orph_evaluate_concepts(con, doc, actor_id = "act_test")
  first <- DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM instances_Flag")$n
  orph_evaluate_concepts(con, doc, actor_id = "act_test")
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM instances_Flag")$n, first)
})

test_that("a rejected instance stops raising concept flags", {
  skip_if_no_conceptr()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)
  orph_setup_concepts(con, actor_id = "act_admin")

  contract <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract")$instance_id
  orph_reject_instance(con, contract, "act_test", note = "Duplicate of another record")

  results <- orph_evaluate_concepts(con, doc, actor_id = "act_test")
  contract_results <- results[results$object_type_id == "Contract", ]
  expect_equal(nrow(contract_results), 0)
})

test_that("changing a concept expression adds a version and deprecates the old one", {
  skip_if_no_conceptr()
  con <- new_test_store(); seed_actors(con)
  orph_setup_concepts(con, actor_id = "act_admin")

  bundle <- orph_active_bundle(con)
  # direct_award, not high_value: high_value's SQL comes from a template, so
  # editing sql_expr on it is a bundle error rather than a change. Templated
  # concepts are changed by their parameters -- see test-templates-scores.R.
  idx <- which(vapply(bundle$concept_defs, function(c) identical(c$id, "direct_award"), logical(1)))
  bundle$concept_defs[[idx]]$sql_expr <- "procurement_procedure IN ('direct', 'limited')"
  bundle$version <- "0.2.0"
  orph_register_bundle(con, bundle, actor_id = "act_admin")

  again <- orph_setup_concepts(con, actor_id = "act_admin")
  expect_equal(again$action[again$concept_id == "direct_award"], "new_version")
  expect_equal(again$action[again$concept_id == "missing_signature"], "unchanged")

  versions <- DBI::dbGetQuery(con,
    "SELECT version, status FROM concept_versions WHERE concept_id = 'direct_award' ORDER BY version")
  expect_equal(versions$status, c("deprecated", "active"))
})

test_that("narrative analysis records its result, source and dependencies", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = fake_populator())
  doc <- seed_document(con, root)
  orph_set_llm_provider("local", fake_llm(paste0(
    '{"summary":"A high-value health services agreement with an uncapped indemnity.",',
    '"risk_level":"high","key_issues":[{"issue":"Uncapped indemnity","severity":"high",',
    '"basis":"Clause 12"}],"recommendations":["Negotiate a cap"],"confidence":0.7}')))
  on.exit(orph_set_llm_provider("local", NULL))

  result <- orph_analyse_document(con, doc, tier = "local", actor_id = "act_test")
  expect_equal(result$result$risk_level, "high")
  expect_equal(result$source, "ai_local")
  expect_equal(result$confidence, 0.7)
  expect_equal(result$status, "unconfirmed")
  expect_gt(result$depends_on_instances, 0)

  deps <- DBI::dbGetQuery(con,
    "SELECT COUNT(*) n FROM concept_evaluation_dependencies WHERE evaluation_id = ?",
    params = list(result$evaluation_id))
  expect_equal(deps$n, result$depends_on_instances)
})

test_that("an unrecognised risk level is normalised rather than stored raw", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)
  orph_set_llm_provider("local", fake_llm('{"summary":"x","risk_level":"catastrophic","confidence":0.7}'))
  on.exit(orph_set_llm_provider("local", NULL))
  expect_equal(orph_analyse_document(con, doc, tier = "local")$result$risk_level, "medium")
})

test_that("analysis refuses a document nothing has been extracted from", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id
  expect_error(orph_analyse_document(con, doc, tier = "local"), "Nothing has been extracted")
})

test_that("amending a depended-on instance marks the analysis stale", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)
  orph_set_llm_provider("local", fake_llm('{"summary":"x","risk_level":"high","confidence":0.7}'))
  on.exit(orph_set_llm_provider("local", NULL))
  ev <- orph_analyse_document(con, doc, tier = "local", actor_id = "act_test")

  stale_now <- function() DBI::dbGetQuery(con,
    "SELECT stale, stale_reason FROM concept_evaluations WHERE evaluation_id = ?",
    params = list(ev$evaluation_id))
  expect_equal(stale_now()$stale, 0)

  contract <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract")$instance_id
  orph_amend_instance(con, contract, list(signature_block_present = "yes"), "act_test")

  expect_equal(stale_now()$stale, 1)
  expect_match(stale_now()$stale_reason, "was amended")
})

test_that("rejecting a depended-on instance also marks the analysis stale", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)
  orph_set_llm_provider("local", fake_llm('{"summary":"x","risk_level":"low","confidence":0.7}'))
  on.exit(orph_set_llm_provider("local", NULL))
  ev <- orph_analyse_document(con, doc, tier = "local", actor_id = "act_test")

  company <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Company")$instance_id
  orph_reject_instance(con, company, "act_test")
  expect_equal(DBI::dbGetQuery(con, "SELECT stale FROM concept_evaluations WHERE evaluation_id = ?",
                               params = list(ev$evaluation_id))$stale, 1)
})

test_that("re-analysing supersedes the previous narrative", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)
  orph_set_llm_provider("local", fake_llm('{"summary":"first","risk_level":"low","confidence":0.7}'))
  on.exit(orph_set_llm_provider("local", NULL))
  first <- orph_analyse_document(con, doc, tier = "local", actor_id = "act_test")
  second <- orph_analyse_document(con, doc, tier = "local", actor_id = "act_test")

  rows <- orph_document_evaluations(con, doc, kind = "narrative")
  expect_equal(nrow(rows), 2)
  expect_equal(rows$stale[rows$evaluation_id == first$evaluation_id], 1)
  expect_equal(rows$stale[rows$evaluation_id == second$evaluation_id], 0)
  expect_equal(nrow(orph_document_evaluations(con, doc, kind = "narrative", include_stale = FALSE)), 1)
})

test_that("an analysis is reviewable like any other AI-sourced row", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)
  orph_set_llm_provider("local", fake_llm('{"summary":"first","risk_level":"low","confidence":0.7}'))
  on.exit(orph_set_llm_provider("local", NULL))
  ev <- orph_analyse_document(con, doc, tier = "local", actor_id = "act_test")

  expect_error(orph_review_evaluation(con, ev$evaluation_id, "amended", "act_test"), "result")

  orph_review_evaluation(con, ev$evaluation_id, "amended", "act_test",
                         result = list(summary = "Corrected by a reviewer", risk_level = "medium"))
  row <- DBI::dbGetQuery(con, "SELECT * FROM concept_evaluations WHERE evaluation_id = ?",
                         params = list(ev$evaluation_id))
  expect_equal(row$status, "amended")
  expect_equal(row$source, "human")
  expect_equal(row$confidence, 1.0)
  expect_match(row$result, "Corrected by a reviewer")

  history <- orph_row_history(con, "concept_evaluations", ev$evaluation_id)
  expect_true("amended" %in% history$action)
  expect_match(history$previous_value[history$action == "amended"], "first")
})
