test_that("cloud is disabled until an administrator turns it on", {
  con <- new_test_store()
  expect_equal(orph_cloud_policy(con)$policy, "disabled")
  expect_false(orph_cloud_policy(con)$available)
  # Even an explicit opt-in cannot override a disabled deployment.
  expect_error(orph_assert_cloud_allowed(con, opt_in = TRUE), "disabled for this deployment")
})

test_that("policy alone never authorises a cloud call", {
  con <- new_test_store()
  for (policy in c("per_user", "org_allow")) {
    orph_set_setting(con, "cloud_ai_policy", policy)
    expect_error(orph_assert_cloud_allowed(con, opt_in = FALSE), "explicit per-request opt-in")
    expect_true(orph_assert_cloud_allowed(con, opt_in = TRUE))
  }
})

test_that("an unrecognised stored policy is an error, not a silent allow", {
  con <- new_test_store()
  orph_set_setting(con, "cloud_ai_policy", "anything_goes")
  expect_error(orph_assert_cloud_allowed(con, opt_in = TRUE), "not a recognised policy")
})

test_that("every model call is written to the audit log, including failures", {
  con <- new_test_store()
  old <- orph_set_llm_provider("local", fake_llm('{"ok":true}'))
  on.exit(orph_set_llm_provider("local", old))

  orph_llm_json(con, "local", "sys", "the prompt text", purpose = "test")
  audit <- orph_llm_audit(con)
  expect_equal(nrow(audit), 1)
  expect_equal(audit$tier, "local")
  expect_equal(audit$purpose, "test")
  expect_equal(audit$prompt_chars, nchar("the prompt text"))
  expect_equal(nchar(audit$payload_digest), 64)
  expect_true(is.na(audit$error))

  orph_set_llm_provider("local", function(sp) list(chat = function(up) stop("model down")))
  expect_error(orph_llm_json(con, "local", "sys", "x", purpose = "test2"), "model call failed")
  audit <- orph_llm_audit(con)
  expect_equal(nrow(audit), 2)
  expect_match(audit$error[[1]], "model down")
})

test_that("the audit log records the prompt's size and digest but not its text", {
  con <- new_test_store()
  old <- orph_set_llm_provider("local", fake_llm('{"ok":true}'))
  on.exit(orph_set_llm_provider("local", old))
  secret <- "COMMERCIALLY SENSITIVE CLAUSE TEXT"
  orph_llm_json(con, "local", "sys", secret, purpose = "test")
  stored <- paste(unlist(orph_llm_audit(con)), collapse = " ")
  expect_false(grepl(secret, stored, fixed = TRUE))
})

test_that("JSON is recovered from a reply wrapped in prose or fences", {
  parse <- orpheus:::parse_json_reply
  expect_equal(parse('{"a":1}')$a, 1)
  expect_equal(parse('```json\n{"a":1}\n```')$a, 1)
  expect_equal(parse('Sure! Here it is: {"a":1} Hope that helps.')$a, 1)
  expect_error(parse("no json at all here"), "did not return usable JSON")
})

test_that("excerpt selection prefers pages containing the terms of interest", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  path <- file.path(tempdir(), paste0(as.integer(runif(1, 1, 1e9)), "-pages.txt"))
  writeLines(c("Page one is about scheduling and logistics arrangements.", "\f",
               "Page two contains the indemnity and indemnity again.", "\f",
               "Page three is about stationery supplies and nothing else."), path)
  doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id

  sel <- orph_select_excerpts(con, doc, terms = "indemnity", max_pages = 1)
  expect_equal(sel$pages, 2)
  expect_true(sel$excerpt_only)
  expect_match(sel$text, "indemnity")

  # With no scoring signal, document order wins over an arbitrary pick.
  none <- orph_select_excerpts(con, doc, terms = character(), max_pages = 1)
  expect_equal(none$pages, 1)

  all_pages <- orph_select_excerpts(con, doc, terms = "indemnity", max_pages = 10)
  expect_false(all_pages$excerpt_only)
})

test_that("a cloud extraction is blocked before any document text is prepared", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  path <- write_contract_file()
  doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id
  expect_error(orph_extract(con, doc, tier = "cloud", actor_id = "act_test", opt_in = TRUE),
               "disabled for this deployment")
  # Nothing was sent, so nothing is in the audit log.
  expect_equal(nrow(orph_llm_audit(con, tier = "cloud")), 0)
})
