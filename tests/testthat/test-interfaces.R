# Interfaces are the ontology's claim that several object types can answer the
# same question. These tests hold the claim to account: the promise is checked
# at registration, and the query really does span every implementing type.

test_that("the shipped bundle declares interfaces its types actually satisfy", {
  bundle <- orph_load_bundle(orph_default_bundle_path())
  expect_no_error(orph_validate_bundle(bundle))

  expect_setequal(orph_implementing_types(bundle, "Named"), c("Company", "Person"))
  expect_setequal(orph_implementing_types(bundle, "PageAnchored"),
                  c("Clause", "KeyDate", "MonetaryAmount"))
  # Relationship is an edge, not an extracted instance, so it implements nothing.
  expect_false("Relationship" %in% orph_implementing_types(bundle, "Reviewable"))

  # Every declared property really is present on every implementing type.
  for (iface in bundle$interfaces) {
    required <- orph_interface_property_ids(iface)
    for (tid in orph_implementing_types(bundle, iface$id)) {
      expect_true(all(required %in% orph_property_ids(orph_object_type(bundle, tid))),
                  info = paste(tid, "implements", iface$id))
    }
  }
})

test_that("a type claiming an interface it cannot satisfy is rejected", {
  bundle <- orph_load_bundle()
  # Contract has `name` but no `naive_key`, so it cannot be Named.
  broken <- bundle
  idx <- which(vapply(broken$object_types, function(o) identical(o$id, "Contract"), logical(1)))
  broken$object_types[[idx]]$implements <- list("Named")
  expect_error(orph_validate_bundle(broken), "missing 'naive_key'")

  unknown <- bundle
  unknown$object_types[[idx]]$implements <- list("Imaginary")
  expect_error(orph_validate_bundle(unknown), "unknown interface")
})

test_that("both spellings of the interface property list must agree", {
  bundle <- orph_load_bundle()
  broken <- bundle
  broken$interfaces[[1]]$requiredProperties <- list()
  expect_error(orph_validate_bundle(broken), "disagree")
})

seed_named <- function(con, root, company, person, doc_name) {
  orph_set_populator(function(bundle, source, llm_fn, tier) list(
    entities = list(
      list(instance_id = "a", type_id = "Company", confidence = 0.9,
           source_refs = list(list(source_label = "d", excerpt = "e")),
           properties = list(name = company, role = "supplier")),
      list(instance_id = "b", type_id = "Person", confidence = 0.7,
           source_refs = list(list(source_label = "d", excerpt = "e")),
           properties = list(name = person, job_title = "Officer")),
      list(instance_id = "c", type_id = "Clause", confidence = 0.9,
           source_refs = list(list(source_label = "d", excerpt = "e")),
           properties = list(clause_number = "12", clause_type = "liability", page_no = 3))),
    relationships = list(), amendments = list()))
  path <- write_contract_file(name = doc_name)
  doc <- orph_ingest(con, path, actor_id = "act_test", storage_root = root)$document_id
  orph_extract(con, doc, "local", actor_id = "act_test")
  doc
}

test_that("one interface query spans every implementing type", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  seed_named(con, root, "Meridian Systems Ltd", "Aoife Nolan", "a.txt")

  named <- orph_object_set_by_interface(con, "Named")
  expect_setequal(named$type_id, c("Company", "Person"))
  # Projected to the interface's properties, whichever type the row came from.
  expect_true(all(c("instance_id", "document_id", "name", "naive_key", "status") %in% names(named)))
  expect_false("job_title" %in% names(named))   # Person-only property is not projected
  expect_false("role" %in% names(named))        # Company-only property is not projected

  anchored <- orph_object_set_by_interface(con, "PageAnchored")
  expect_setequal(unique(anchored$type_id), c("Clause", "KeyDate", "MonetaryAmount"))
})

test_that("interface queries respect review status, scope and extra predicates", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  doc1 <- seed_named(con, root, "Alpha Ltd", "Bea Ryan", "a.txt")
  doc2 <- seed_named(con, root, "Gamma Ltd", "Dee Walsh", "b.txt")

  expect_equal(nrow(orph_object_set_by_interface(con, "Named")), 4)
  expect_equal(nrow(orph_object_set_by_interface(con, "Named", document_id = doc1)), 2)

  low <- orph_object_set_by_interface(con, "Reviewable", where = "confidence <= 0.7")
  expect_true(all(low$confidence <= 0.7))
  expect_true("Person" %in% low$type_id)

  target <- orph_object_set_by_interface(con, "Named", document_id = doc2)$instance_id[[1]]
  orph_reject_instance(con, target, "act_test")
  expect_equal(nrow(orph_object_set_by_interface(con, "Named")), 3)
  expect_equal(nrow(orph_object_set_by_interface(con, "Named", include_rejected = TRUE)), 4)
})

test_that("an unknown interface fails with the known ones listed", {
  con <- new_test_store(); seed_actors(con)
  err <- tryCatch(orph_object_set_by_interface(con, "Nope"), error = function(e) e)
  expect_match(conditionMessage(err), "No interface")
  expect_match(conditionMessage(err), "Reviewable")
})

test_that("corpus matching finds a name shared across different Named types", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  # The same name is a company in one document and a person in another --
  # exactly the shape worth surfacing, and invisible to a same-type-only search.
  doc1 <- seed_named(con, root, "Byrne Holdings", "Aoife Nolan", "a.txt")
  doc2 <- seed_named(con, root, "Unrelated Ltd",  "Byrne Holdings", "b.txt")

  result <- orph_corpus_analysis(con, doc1, actor_id = "act_test")
  match <- Filter(function(m) m$name == "Byrne Holdings", result$counterparties)[[1]]

  # No other Company shares the name, so the same-type count stays zero...
  expect_equal(match$appears_in_documents, 0)
  # ...but the cross-type hit is reported rather than dropped.
  expect_equal(length(match$cross_type_matches), 1)
  expect_equal(match$cross_type_matches[[1]]$type_id, "Person")
  expect_equal(match$cross_type_matches[[1]]$document_id, doc2)
})
