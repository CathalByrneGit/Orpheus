# The two files are built by string concatenation, which is fine for a fixed
# template and not fine for the parts that come from a bundle. These tests hold
# them to being parseable YAML, and to the split Datasette 1.0 requires.

generate_pair <- function(bundle = orph_load_bundle(), ...) {
  dir <- withr::local_tempdir()
  paths <- orph_write_datasette_config(file.path(dir, "datasette.yml"),
                                       bundle = bundle, ...)
  lapply(paths, yaml::read_yaml)
}

test_that("both generated files are valid YAML", {
  skip_if_not_installed("yaml")
  out <- generate_pair()
  expect_true(is.list(out$config))
  expect_true(is.list(out$metadata))
})

test_that("canned queries live in the config file, never in the metadata file", {
  # Datasette 1.0 reads queries out of --config only. A query left in the
  # metadata file loses its `sql` on the way through and the server dies at
  # startup with a KeyError, which is how this was found.
  skip_if_not_installed("yaml")
  out <- generate_pair()

  queries <- out$config$databases$orpheus$queries
  expect_true(length(queries) > 0)
  for (name in names(queries)) {
    expect_true(!is.null(queries[[name]]$sql), info = name)
  }
  expect_null(out$metadata$databases$orpheus$queries)
})

test_that("descriptions live in the metadata file, never in the config file", {
  # The mirror of the rule above: only --metadata reaches the rendered pages.
  skip_if_not_installed("yaml")
  out <- generate_pair()

  tables <- out$metadata$databases$orpheus$tables
  expect_true(!is.null(tables$documents$description))
  for (tbl in out$config$databases$orpheus$tables) {
    expect_null(tbl$description)
  }
})

test_that("the title comes from the bundle, and a colon in it stays parseable", {
  skip_if_not_installed("yaml")
  bundle <- orph_load_bundle()
  expect_equal(generate_pair(bundle)$metadata$title,
               paste("Orpheus:", bundle$bundle_name))

  bundle$bundle_name <- 'Planning: "core" set'
  expect_match(generate_pair(bundle)$metadata$title, "Planning", fixed = TRUE)
})

test_that("the API token is named, not written", {
  skip_if_not_installed("yaml")
  plugin <- generate_pair()$config$plugins$`orpheus-datasette`
  expect_equal(plugin$token, list(`$env` = "ORPHEUS_API_TOKEN"))
})

test_that("neither file tells people to use --immutable", {
  # It is the flag that silently empties a live WAL store, and the generated
  # file used to claim the deployment ran with it.
  dir <- withr::local_tempdir()
  paths <- orph_write_datasette_config(file.path(dir, "datasette.yml"))
  for (p in paths) {
    expect_false(any(grepl("immutable", readLines(p), fixed = TRUE)), info = p)
  }
})

test_that("the accuracy query spans every instance table in the bundle", {
  # A query naming one domain's tables would report on part of the store the
  # moment a bundle added a type.
  skip_if_not_installed("yaml")
  bundle <- orph_load_bundle()
  sql <- generate_pair(bundle)$config$databases$orpheus$
    queries$extraction_accuracy_by_confidence$sql
  for (ot in managed_object_types(bundle)) {
    expect_true(grepl(ot$table_name, sql, fixed = TRUE), info = ot$table_name)
  }
})
