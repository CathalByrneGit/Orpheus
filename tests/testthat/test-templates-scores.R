# Concept templates make a policy number a deployment setting rather than a
# guess wearing the authority of code. Composite scores give a deterministic
# counterpart to the model's risk reading. Both go through conceptR.

test_that("a templated concept renders its default into real SQL", {
  skip_if_no_conceptr()
  con <- new_test_store(); seed_actors(con)

  params <- orph_concept_parameters(con)
  expect_equal(params$template_id, "value_threshold")
  expect_equal(params$parameter, "threshold")
  expect_equal(params$source, "bundle_default")
  expect_equal(params$effective, "1000000")

  orph_setup_concepts(con, actor_id = "act_admin")
  sql <- DBI::dbGetQuery(con,
    "SELECT sql_expr FROM concept_versions WHERE concept_id = 'high_value' AND status = 'active'")$sql_expr
  expect_match(sql, "1000000", fixed = TRUE)
  expect_false(grepl("\\{\\{", sql))
})

test_that("a rendered threshold never reaches SQL in scientific notation", {
  # 5e+06 parses, but a threshold that reads as 5e+06 in the audit trail is a
  # threshold nobody checks.
  expect_equal(orpheus:::format_param(5000000), "5000000")
  expect_equal(orpheus:::format_param(1e9), "1000000000")
  expect_equal(orpheus:::format_param(0.5), "0.5")
  expect_equal(orpheus:::format_param("open"), "open")
})

test_that("overriding a parameter adds a concept version rather than editing one", {
  skip_if_no_conceptr()
  con <- new_test_store(); seed_actors(con)
  orph_setup_concepts(con, actor_id = "act_admin")

  orph_set_concept_parameter(con, "value_threshold", "threshold", 5000000, "act_admin")

  versions <- DBI::dbGetQuery(con,
    "SELECT version, status, sql_expr FROM concept_versions
     WHERE concept_id = 'high_value' ORDER BY version")
  expect_equal(nrow(versions), 2)
  # The old version is deprecated, not deleted: an evaluation made under it
  # still points at a version that exists.
  expect_equal(versions$status, c("deprecated", "active"))
  expect_match(versions$sql_expr[[1]], "1000000", fixed = TRUE)
  expect_match(versions$sql_expr[[2]], "5000000", fixed = TRUE)

  params <- orph_concept_parameters(con)
  expect_equal(params$source, "deployment_override")
  expect_equal(params$effective, "5000000")
  expect_equal(params$default, "1000000")     # the bundle default is unchanged

  expect_true("concept_parameter_changed" %in%
              DBI::dbGetQuery(con, "SELECT action FROM edit_history")$action)
})

test_that("an unknown template or parameter is refused with the options listed", {
  con <- new_test_store(); seed_actors(con)
  expect_error(orph_set_concept_parameter(con, "nope", "threshold", 1, "act_admin"),
               "No concept template")
  err <- tryCatch(orph_set_concept_parameter(con, "value_threshold", "nope", 1, "act_admin"),
                  error = function(e) e)
  expect_match(conditionMessage(err), "no parameter")
  expect_match(conditionMessage(err), "threshold")
})

seed_risky_contract <- function(con, root, ...) {
  orph_set_populator(function(bundle, source, llm_fn, tier) list(
    entities = list(list(instance_id = "c", type_id = "Contract", confidence = 0.9,
      source_refs = list(list(source_label = "d", excerpt = "AGREEMENT")),
      properties = list(name = "Services Agreement", value_amount = 2400000,
                        value_currency = "EUR", signature_block_present = "no",
                        procurement_procedure = "direct award"))),
    relationships = list(), amendments = list()))
  path <- write_contract_file(...)
  doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id
  orph_extract(con, doc, "local", actor_id = "act_test")
  orph_setup_concepts(con, actor_id = "act_admin")
  orph_setup_scores(con, actor_id = "act_admin")
  orph_evaluate_concepts(con, doc, actor_id = "act_test")
  doc
}

test_that("a composite score decomposes into the concepts that produced it", {
  skip_if_no_conceptr()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  doc <- seed_risky_contract(con, root)

  result <- orph_evaluate_score(con, doc, actor_id = "act_test")$results[[1]]
  expect_equal(result$score, 6)          # 2 + 2 + 1 + 1, everything fires
  expect_equal(result$max_possible, 6)
  expect_equal(result$tier, "high")

  fired <- vapply(result$contributions, function(c) c$concept_id, character(1))
  expect_setequal(fired, c("missing_signature", "direct_award",
                           "high_value", "open_ended_term"))
  # A score nobody can decompose is no better than the model's opinion.
  weights <- vapply(result$contributions, function(c) c$weight, numeric(1))
  expect_equal(sum(weights), result$score)
})

test_that("changing a threshold changes the score, through a new concept version", {
  skip_if_no_conceptr()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  doc <- seed_risky_contract(con, root)
  before <- orph_evaluate_score(con, doc, actor_id = "act_test")$results[[1]]
  expect_equal(before$score, 6)

  # The contract is worth 2.4m, so a 5m threshold should stop high_value firing.
  orph_set_concept_parameter(con, "value_threshold", "threshold", 5000000, "act_admin")
  orph_evaluate_concepts(con, doc, actor_id = "act_test")
  after <- orph_evaluate_score(con, doc, actor_id = "act_test")$results[[1]]

  expect_equal(after$score, 5)
  expect_false("high_value" %in% vapply(after$contributions, function(c) c$concept_id, character(1)))

  # The score's component must track the new concept version, or it would keep
  # scoring against the old threshold.
  pinned <- DBI::dbGetQuery(con,
    "SELECT version FROM composite_score_components WHERE concept_id = 'high_value'")$version
  active <- DBI::dbGetQuery(con,
    "SELECT version FROM concept_versions WHERE concept_id = 'high_value' AND status = 'active'")$version
  expect_equal(pinned, active)
})

test_that("the score is stored as a reviewable evaluation with its dependency", {
  skip_if_no_conceptr()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  doc <- seed_risky_contract(con, root)
  orph_evaluate_score(con, doc, actor_id = "act_test")

  row <- DBI::dbGetQuery(con,
    "SELECT * FROM concept_evaluations WHERE kind = 'score' AND target_document_id = ?",
    params = list(doc))
  expect_equal(nrow(row), 1)
  expect_equal(row$concept_id, "contract_risk")
  expect_equal(row$status, "unconfirmed")
  expect_equal(row$source, "ai_local")

  # Amending the contract must mark the score stale, like any other evaluation.
  contract <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract")$instance_id
  orph_amend_instance(con, contract, list(signature_block_present = "yes"), "act_test")
  expect_equal(DBI::dbGetQuery(con,
    "SELECT stale FROM concept_evaluations WHERE evaluation_id = ?",
    params = list(row$evaluation_id))$stale, 1)
})

test_that("score and narrative disagreement is reported rather than reconciled", {
  skip_if_no_conceptr()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  doc <- seed_risky_contract(con, root)

  expect_false(orph_risk_comparison(con, doc)$available)   # neither run yet

  orph_evaluate_score(con, doc, actor_id = "act_test")
  orph_set_llm_provider("local", fake_llm('{"summary":"Routine.","risk_level":"low","confidence":0.7}'))
  orph_analyse_document(con, doc, tier = "local", actor_id = "act_test")

  comparison <- orph_risk_comparison(con, doc)
  expect_true(comparison$available)
  expect_equal(comparison$score_tier, "high")
  expect_equal(comparison$narrative_level, "low")
  expect_false(comparison$agree)
  # Neither reading is asserted to be the correct one.
  expect_match(comparison$note, "Neither is")
})

test_that("evaluating a score needs something to score", {
  skip_if_no_conceptr()
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  orph_setup_concepts(con, actor_id = "act_admin")
  orph_setup_scores(con, actor_id = "act_admin")
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id
  expect_error(orph_evaluate_score(con, doc, actor_id = "act_test"), "Run an extraction pass")
})

test_that("a concept cannot draw its SQL from two places at once", {
  bundle <- orph_load_bundle()
  idx <- which(vapply(bundle$concept_defs, function(c) identical(c$id, "high_value"), logical(1)))

  # Setting sql_expr on a templated concept would otherwise be silently ignored.
  both <- bundle
  both$concept_defs[[idx]]$sql_expr <- "1 = 1"
  expect_error(orph_validate_bundle(both), "exactly one")

  neither <- bundle
  neither$concept_defs[[idx]]$template_id <- NULL
  expect_error(orph_validate_bundle(neither), "neither sql_expr nor template_id")

  unknown <- bundle
  unknown$concept_defs[[idx]]$template_id <- "imaginary"
  expect_error(orph_validate_bundle(unknown), "unknown template")
})
