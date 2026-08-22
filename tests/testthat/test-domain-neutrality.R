# The engine is domain-neutral and the contract bundle is an example. That is
# easy to claim and easy to quietly break, so it is tested: a bundle from an
# unrelated domain runs the same pipeline with no code changes.

planning_bundle <- function() {
  prop <- function(id, type, description, nullable = TRUE, values = NULL) {
    list(id = id, type = type, nullable = nullable, description = description,
         source = list(column = id),
         values = if (is.null(values)) NULL else as.list(values))
  }
  provenance <- list(
    prop("document_id", "string", "Document"),
    prop("source", "string", "Provenance"),
    prop("confidence", "double", "Rubric level"),
    prop("status", "string", "Review status"),
    prop("amended_by", "string", "Amended by"),
    prop("amended_at", "string", "Amended at"))

  object_type <- function(id, props, implements = list()) {
    list(id = id, display_name = id, description = id,
         implements = implements,
         table_name = paste0("instances_", id), primary_key = "instance_id",
         primaryKey = "instance_id",
         source = list(kind = "table", table = paste0("instances_", id)),
         properties = c(list(prop("instance_id", "string", "Id", nullable = FALSE)),
                        props, provenance),
         x_orpheus = list(managed = TRUE))
  }

  objects <- list(
    object_type("Application", list(
      prop("name", "string", "Application title", nullable = FALSE),
      prop("reference", "string", "Planning reference"),
      prop("floor_area", "double", "Proposed floor area in square metres"),
      prop("area_unit", "string", "Unit of the floor area"),
      prop("decision", "string", "Outcome", values = c("granted", "refused", "withdrawn")))),
    object_type("Applicant", list(
      prop("name", "string", "Applicant name", nullable = FALSE),
      prop("naive_key", "string", "Normalised name")), implements = list("Named")),
    object_type("Condition", list(
      prop("application_instance_id", "string", "Application this attaches to"),
      prop("text", "string", "Condition text"),
      prop("page_no", "integer", "Page")))
  )

  interfaces <- list(list(
    id = "Named", display_name = "Named", description = "Has a matchable name",
    properties = list(prop("instance_id", "string", "Id", nullable = FALSE),
                      prop("document_id", "string", "Document"),
                      prop("name", "string", "Name", nullable = FALSE),
                      prop("naive_key", "string", "Key"),
                      prop("status", "string", "Status")),
    requiredProperties = list(prop("instance_id", "string", "Id", nullable = FALSE),
                              prop("document_id", "string", "Document"),
                              prop("name", "string", "Name", nullable = FALSE),
                              prop("naive_key", "string", "Key"),
                              prop("status", "string", "Status"))))

  links <- list(list(
    id = "has_condition", display_name = "has condition", description = "",
    from = "Application", to = "Condition",
    from_type_id = "Application", to_type_id = "Condition",
    cardinality = "one-to-many", directed = TRUE,
    join = list(fromKeys = list("instance_id"),
                toKeys = list("application_instance_id"))))

  list(
    bundle_id = "planning-core", bundle_name = "Planning applications",
    version = "0.1.0",
    # The whole of the domain knowledge the engine needs.
    x_orpheus = list(
      primary_object_type = "Application",
      container_property  = "application_instance_id",
      value_property      = "floor_area",
      currency_property   = "area_unit",
      document_types      = list("application", "decision", "objection", "other")),
    object_types = objects, interfaces = interfaces, link_types = links,
    action_types = list(),
    concept_defs = list(list(
      id = "large_development", object_type_id = "Application", scope = "planning",
      display_name = "Large development",
      description = "Floor area at or above the threshold.",
      sql_expr = "floor_area IS NOT NULL AND CAST(floor_area AS REAL) >= 1000",
      rationale = "Placeholder threshold.")))
}

test_that("a bundle from an unrelated domain validates and generates its schema", {
  bundle <- planning_bundle()
  expect_no_error(orph_validate_bundle(bundle))

  path <- tempfile(fileext = ".sqlite")
  con <- orph_init_store(path, bundle = bundle)
  withr::defer({
    try(orph_disconnect(con), silent = TRUE)
    unlink(paste0(path, c("", "-wal", "-shm", ".writer.lock")))
  })

  for (tbl in c("instances_Application", "instances_Applicant", "instances_Condition")) {
    expect_true(DBI::dbExistsTable(con, tbl), info = tbl)
  }
  expect_false(DBI::dbExistsTable(con, "instances_Contract"))
  expect_equal(orph_domain(orph_active_bundle(con))$primary_object_type, "Application")
  expect_true("application" %in% orph_document_types(orph_active_bundle(con)))
})

test_that("extraction, linking and review work unchanged in that domain", {
  bundle <- planning_bundle()
  path <- tempfile(fileext = ".sqlite")
  con <- orph_init_store(path, bundle = bundle)
  root <- test_storage_root()
  withr::defer({
    orph_set_populator(NULL); orph_set_llm_provider("local", NULL)
    try(orph_disconnect(con), silent = TRUE)
    unlink(paste0(path, c("", "-wal", "-shm", ".writer.lock")))
  })
  orph_create_actor(con, "Planner", actor_id = "act_test")

  orph_set_populator(function(bundle, source, llm_fn, tier) list(
    entities = list(
      list(instance_id = "a", type_id = "Application", confidence = 0.9,
           source_refs = list(list(source_label = "d", excerpt = "PLANNING APPLICATION")),
           properties = list(name = "Extension at 4 Main St", reference = "P-2024-118",
                             floor_area = 1450, area_unit = "sqm", decision = "granted")),
      list(instance_id = "b", type_id = "Applicant", confidence = 0.9,
           source_refs = list(list(source_label = "d", excerpt = "Byrne Developments")),
           properties = list(name = "Byrne Developments Ltd")),
      list(instance_id = "c", type_id = "Condition", confidence = 0.7,
           source_refs = list(list(source_label = "d", excerpt = "Condition 3")),
           properties = list(text = "Works shall not begin before 8am.", page_no = 2))),
    relationships = list(), amendments = list()))

  file <- file.path(tempdir(), paste0(as.integer(runif(1, 1, 1e9)), "-planning.txt"))
  writeLines(c("PLANNING APPLICATION P-2024-118",
               "Applicant: Byrne Developments Ltd.",
               "Proposed floor area 1,450 sqm. Decision granted on 1 March 2025."), file)
  doc <- orph_ingest(con, file, actor_id = "act_test", storage_root = root)$document_id
  result <- orph_extract(con, doc, "local", actor_id = "act_test")
  expect_equal(result$n_entities, 3)

  # The deterministic pass attached its findings to the Application, using the
  # container property this bundle declared -- no code knows what a planning
  # application is.
  app <- DBI::dbGetQuery(con, "SELECT instance_id FROM instances_Application")$instance_id
  linked <- DBI::dbGetQuery(con,
    "SELECT application_instance_id FROM instances_Condition")$application_instance_id
  expect_equal(linked, app)

  # Review works the same.
  orph_amend_instance(con, app, list(decision = "refused"), "act_test")
  expect_equal(DBI::dbGetQuery(con, "SELECT status FROM instances_Application")$status, "amended")

  # And so does the codelist report, on this bundle's own codelist.
  orph_amend_instance(con, app, list(decision = "deferred"), "act_test")
  violations <- orph_codelist_violations(con)
  expect_equal(violations$value, "deferred")
  expect_equal(violations$property, "decision")
})

test_that("the interface query spans this domain's own Named types", {
  bundle <- planning_bundle()
  path <- tempfile(fileext = ".sqlite")
  con <- orph_init_store(path, bundle = bundle)
  withr::defer({
    try(orph_disconnect(con), silent = TRUE)
    unlink(paste0(path, c("", "-wal", "-shm", ".writer.lock")))
  })
  expect_equal(orph_implementing_types(orph_active_bundle(con), "Named"), "Applicant")
  expect_equal(nrow(orph_object_set_by_interface(con, "Named")), 0)
})

test_that("a domain with no comparable value says so instead of failing", {
  bundle <- planning_bundle()
  bundle$x_orpheus$value_property <- NULL
  bundle$x_orpheus$currency_property <- NULL
  expect_no_error(orph_validate_bundle(bundle))

  path <- tempfile(fileext = ".sqlite")
  con <- orph_init_store(path, bundle = bundle)
  withr::defer({
    try(orph_disconnect(con), silent = TRUE)
    unlink(paste0(path, c("", "-wal", "-shm", ".writer.lock")))
  })
  comparison <- orpheus:::compare_primary_values(con, orph_active_bundle(con), "doc_x", list())
  expect_false(comparison$available)
  expect_match(comparison$reason, "no comparable value")
})
