test_that("the shipped bundle satisfies all three consumers' field conventions", {
  bundle <- orph_load_bundle(orph_default_bundle_path())
  expect_no_error(orph_validate_bundle(bundle))
  for (ot in bundle$object_types) {
    expect_false(is.null(ot$table_name), info = ot$id)   # conceptR
    expect_false(is.null(ot$primary_key), info = ot$id)  # conceptR
    expect_false(is.null(ot$source$table), info = ot$id) # objectSetsR
    expect_identical(ot$table_name, ot$source$table, info = ot$id)
  }
  for (lt in bundle$link_types) {
    expect_identical(lt$from, lt$from_type_id, info = lt$id)  # objectSetsR vs ontologyDiscoverR
    expect_identical(lt$to, lt$to_type_id, info = lt$id)
    expect_false(is.null(lt$join$fromKeys), info = lt$id)     # objectSetsR traversal
  }
})

test_that("validation rejects the ways a bundle can be quietly wrong", {
  bundle <- orph_load_bundle()

  broken <- bundle; broken$object_types[[1]]$table_name <- NULL
  expect_error(orph_validate_bundle(broken), "table_name")

  broken <- bundle; broken$object_types[[1]]$source$table <- "somewhere_else"
  expect_error(orph_validate_bundle(broken), "disagree")

  broken <- bundle; broken$link_types[[1]]$to <- "NoSuchType"
  expect_error(orph_validate_bundle(broken), "unknown object type")

  broken <- bundle
  broken$object_types[[1]]$properties <- Filter(
    function(p) !identical(p$id, "status"), broken$object_types[[1]]$properties)
  expect_error(orph_validate_bundle(broken), "exclude rejected rows")
})

test_that("registering a bundle creates one table per managed object type", {
  con <- new_test_store()
  bundle <- orph_active_bundle(con)
  expect_equal(bundle$bundle_id, "contract-core")

  for (ot in bundle$object_types) {
    if (identical(ot$x_orpheus$managed, FALSE)) next
    expect_true(DBI::dbExistsTable(con, ot$table_name), info = ot$id)
    expect_true(all(orph_property_ids(ot) %in% DBI::dbListFields(con, ot$table_name)),
                info = ot$id)
  }
})

test_that("Relationship is backed by the hand-written edges table, not generated", {
  con <- new_test_store()
  bundle <- orph_active_bundle(con)
  rel <- orph_object_type(bundle, "Relationship")
  expect_equal(rel$table_name, "edges")
  expect_false(DBI::dbExistsTable(con, "instances_Relationship"))
})

test_that("applying a bundle is idempotent but adds newly declared columns", {
  con <- new_test_store()
  bundle <- orph_active_bundle(con)
  expect_length(orph_apply_bundle_schema(con, bundle), 0)

  idx <- which(vapply(bundle$object_types, function(o) identical(o$id, "Contract"), logical(1)))
  bundle$object_types[[idx]]$properties <- c(
    bundle$object_types[[idx]]$properties,
    list(list(id = "renewal_notice_days", type = "integer", nullable = TRUE,
              description = "x", source = list(column = "renewal_notice_days"))))
  expect_equal(orph_apply_bundle_schema(con, bundle), "instances_Contract")
  expect_true("renewal_notice_days" %in% DBI::dbListFields(con, "instances_Contract"))
})

test_that("a staging bundle is stored but cannot be activated", {
  con <- new_test_store()
  bundle <- orph_active_bundle(con)
  bundle$version <- "0.2.0-staging"
  expect_error(orph_register_bundle(con, bundle, activate = TRUE, stage = "staging"),
               "cannot be activated")
  expect_no_error(orph_register_bundle(con, bundle, activate = FALSE, stage = "staging"))
  expect_equal(orph_active_bundle(con)$version, "0.1.0")
})
