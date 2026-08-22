test_that("confirming records the transition without touching the values", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  seed_document(con, root)
  id <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract")$instance_id

  orph_confirm_instance(con, id, "act_test")
  row <- DBI::dbGetQuery(con, "SELECT * FROM instances_Contract")
  expect_equal(row$status, "confirmed")
  expect_equal(row$amended_by, "act_test")
  expect_equal(row$source, "ai_local")   # still machine-sourced; a human agreed with it
  expect_equal(row$name, "Services Agreement")
})

test_that("amending preserves the previous value and reattributes the row to the human", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  seed_document(con, root)
  id <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Company")$instance_id

  orph_amend_instance(con, id, list(name = "Meridian Systems Ltd.", role = "prime_supplier"),
                      "act_test", note = "Name per the register")

  row <- DBI::dbGetQuery(con, "SELECT * FROM instances_Company")
  expect_equal(row$status, "amended")
  expect_equal(row$source, "human")
  expect_equal(row$confidence, 1.0)      # a human correction is ground truth
  expect_equal(row$naive_key, "meridian systems")  # derived value follows the edit

  history <- orph_row_history(con, "instances_Company", id)
  amend <- history[history$action == "amend", ]
  expect_equal(nrow(amend), 1)
  expect_match(amend$previous_value, "Meridian Systems Limited")
  expect_match(amend$new_value, "prime_supplier")
  expect_equal(amend$note, "Name per the register")
})

test_that("the audit trail orders by sequence, not by a same-second timestamp", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  seed_document(con, root)
  id <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Company")$instance_id
  orph_amend_instance(con, id, list(role = "a"), "act_test")
  orph_amend_instance(con, id, list(role = "b"), "act_test")

  history <- orph_row_history(con, "instances_Company", id)
  expect_equal(history$action, c("extract", "amend", "amend"))
  expect_true(all(diff(history$seq) > 0))
  expect_match(history$new_value[[3]], '"b"')
})

test_that("rejecting excludes a row without deleting it", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)
  id <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Company")$instance_id

  orph_reject_instance(con, id, "act_test", note = "Not a party to this agreement")
  expect_equal(DBI::dbGetQuery(con, "SELECT status FROM instances_Company")$status, "rejected")
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM instances_Company")$n, 1)

  visible <- orph_document_instances(con, doc)
  expect_false(id %in% visible$instance_id)
  expect_true(id %in% orph_document_instances(con, doc, include_rejected = TRUE)$instance_id)
})

test_that("amending a property the bundle does not declare is refused with a route forward", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  seed_document(con, root)
  id <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Company")$instance_id
  expect_error(orph_amend_instance(con, id, list(vat_number = "IE123"), "act_test"),
               "schema amendment")
})

test_that("review actions require a named actor", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  seed_document(con, root)
  id <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Company")$instance_id
  expect_error(orph_confirm_instance(con, id, NULL), "actor_id")
  expect_error(orph_amend_instance(con, id, list(role = "x"), ""), "actor_id")
})

test_that("edges are reviewable and a corrected link type is validated", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  seed_document(con, root)
  edge <- DBI::dbGetQuery(con, "SELECT edge_id FROM edges")$edge_id

  orph_review_edge(con, edge, "confirmed", "act_test")
  expect_equal(DBI::dbGetQuery(con, "SELECT status FROM edges")$status, "confirmed")

  expect_error(orph_review_edge(con, edge, actor_id = "act_test", link_type_id = "not_a_link"),
               "not a link type")

  orph_review_edge(con, edge, actor_id = "act_test", link_type_id = "subcontracts_to")
  row <- DBI::dbGetQuery(con, "SELECT * FROM edges")
  expect_equal(row$link_type_id, "subcontracts_to")
  expect_equal(row$status, "amended")
  expect_equal(row$source, "human")
})

test_that("the document-level flag is separate from per-instance status", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con); use_fakes()
  doc <- seed_document(con, root)

  before <- orph_review_progress(con, doc)
  expect_gt(before$unconfirmed, 0)
  expect_equal(before$confirmed, 0)

  result <- orph_mark_document_reviewed(con, doc, "act_test")
  expect_equal(result$review_status, "reviewed")
  # Marking the document reviewed must not silently confirm its instances.
  expect_equal(orph_review_progress(con, doc)$unconfirmed, before$unconfirmed)
  expect_match(result$note, "still unconfirmed")

  reopened <- orph_mark_document_reviewed(con, doc, "act_test", reviewed = FALSE)
  expect_equal(reopened$review_status, "unreviewed")
  expect_true(is.na(orph_get_document(con, doc)$reviewed_at))
})

test_that("accepting a new_property amendment adds the column and bumps the bundle", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = fake_populator(extra_properties = list(renewal_notice_period = "90 days")))
  seed_document(con, root)

  queue <- orph_schema_amendments(con)
  am <- queue[queue$amendment_type == "new_property", ]
  result <- orph_review_schema_amendment(con, am$amendment_id, "accepted", "act_admin")

  expect_true(result$applied_to_bundle)
  expect_equal(result$bundle_version, "0.1.1")
  expect_true("renewal_notice_period" %in% DBI::dbListFields(con, "instances_Contract"))
  expect_equal(orph_active_bundle(con)$version, "0.1.1")

  # And the property is now amendable, where it was refused before.
  id <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Contract")$instance_id
  expect_no_error(orph_amend_instance(con, id, list(renewal_notice_period = "90 days"), "act_test"))
})

test_that("an accepted amendment leaves every alias spelling in agreement", {
  # The bundle carries each list under two keys, one per consumer package. The
  # mirrors used to be maintained by hand at the point of amendment, so an
  # accepted property reached `object_types` and `objects` but not the rest.
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = fake_populator(extra_properties = list(renewal_notice_period = "90 days")))
  seed_document(con, root)

  am <- orph_schema_amendments(con)
  am <- am[am$amendment_type == "new_property", ]
  orph_review_schema_amendment(con, am$amendment_id, "accepted", "act_admin")

  bundle <- orph_active_bundle(con)
  expect_identical(bundle$objects, bundle$object_types)
  expect_identical(bundle$links, bundle$link_types)
  expect_identical(bundle$interfaceTypes, bundle$interfaces)
  expect_identical(bundle$concepts, bundle$concept_defs)

  # And the amendment really did land, so the agreement is not vacuous.
  contract <- bundle$objects[[which(vapply(bundle$objects,
    function(ot) identical(ot$id, "Contract"), logical(1)))]]
  expect_true("renewal_notice_period" %in% orph_property_ids(contract))
})

test_that("a new_type amendment is recorded but not auto-applied", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = fake_populator(extra_entities = list(
    list(instance_id = "e9", type_id = "Vessel", confidence = 0.5,
         source_refs = list(list(source_label = "x", excerpt = "y")),
         properties = list(name = "MV Something")))))
  seed_document(con, root)

  am <- orph_schema_amendments(con)
  am <- am[am$amendment_type == "new_type", ]
  result <- orph_review_schema_amendment(con, am$amendment_id, "accepted", "act_admin")
  expect_false(result$applied_to_bundle)
  expect_match(result$note, "not applied automatically")
  expect_equal(orph_active_bundle(con)$version, "0.1.0")
})

test_that("a decided amendment cannot be decided twice", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  use_fakes(populator = fake_populator(extra_properties = list(x_field = "v")))
  seed_document(con, root)
  am <- orph_schema_amendments(con)[1, ]
  orph_review_schema_amendment(con, am$amendment_id, "rejected", "act_admin")
  expect_error(orph_review_schema_amendment(con, am$amendment_id, "accepted", "act_admin"),
               "already")
})
