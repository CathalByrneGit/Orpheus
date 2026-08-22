# The CUAD harness. The real dataset is not shipped, so these run against a
# synthetic file in CUAD's own SQuAD shape -- which also pins the loader to the
# format rather than to one particular download.

write_fake_cuad <- function(categories = c("Governing Law", "Cap On Liability"),
                            include_absent = TRUE) {
  path <- file.path(tempdir(), paste0(orpheus:::orph_id("cuad"), ".json"))

  contract_text <- paste(
    "MASTER SERVICES AGREEMENT",
    "This Agreement is governed by the laws of Ireland.",
    "The aggregate liability of the Supplier shall not exceed the Charges paid.",
    "The Supplier shall indemnify the Authority without limitation.",
    sep = "\n")

  qas <- list(
    list(question = 'Highlight the parts related to "Governing Law" that should be reviewed.',
         answers = list(list(text = "This Agreement is governed by the laws of Ireland.",
                             answer_start = 26L))),
    list(question = 'Highlight the parts related to "Cap On Liability" that should be reviewed.',
         answers = list(list(text = "The aggregate liability of the Supplier shall not exceed the Charges paid.",
                             answer_start = 77L)))
  )
  if (include_absent) {
    # A category with no answers is a true negative, not missing data.
    qas <- c(qas, list(list(
      question = 'Highlight the parts related to "Audit Rights" that should be reviewed.',
      answers = list())))
  }

  doc <- list(data = list(list(
    title = "Test-MSA",
    paragraphs = list(list(context = contract_text, qas = qas)))))
  writeLines(jsonlite::toJSON(doc, auto_unbox = TRUE), path)
  path
}

test_that("the loader reads CUAD's SQuAD shape and derives its own vocabulary", {
  cuad <- orph_load_cuad(write_fake_cuad())

  expect_equal(nrow(cuad$contracts), 1)
  expect_equal(cuad$contracts$title, "Test-MSA")
  # The category set comes from the file. Nothing is hardcoded: a filtered or
  # extended CUAD has a different vocabulary and must score against its own.
  expect_setequal(cuad$categories, c("Governing Law", "Cap On Liability", "Audit Rights"))

  present <- cuad$labels[cuad$labels$present, ]
  expect_equal(nrow(present), 2)
  expect_match(present$text[present$category == "Governing Law"], "laws of Ireland")

  absent <- cuad$labels[!cuad$labels$present, ]
  expect_equal(absent$category, "Audit Rights")
})

test_that("category names are recovered from CUAD's long questions", {
  expect_equal(orpheus:::cuad_category('Highlight the parts related to "Governing Law" that matter.'),
               "Governing Law")
  # A question with no quoted category falls back to the question itself rather
  # than silently producing an empty category that would group unrelated labels.
  expect_equal(orpheus:::cuad_category("no quoted category here"), "no quoted category here")
})

test_that("a missing or empty CUAD file fails clearly", {
  expect_error(orph_load_cuad("/no/such/cuad.json"), "No CUAD file")
  empty <- file.path(tempdir(), "empty-cuad.json")
  writeLines('{"data": []}', empty)
  expect_error(orph_load_cuad(empty), "no entries")
})

test_that("the shipped mapping targets only clause types the bundle knows", {
  mapping <- orph_load_cuad_map()
  expect_gt(length(mapping), 0)

  bundle <- orph_load_bundle()
  clause <- orph_object_type(bundle, "Clause")
  allowed <- unlist(Filter(function(p) identical(p$id, "clause_type"),
                           clause$properties)[[1]]$values)
  # If these drift apart the benchmark scores against a vocabulary the
  # extractor was never asked to produce, and every category reads as a miss.
  expect_true(all(unname(mapping) %in% allowed))
})

test_that("scoring counts a labelled span as found when the clause overlaps it", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  cuad <- orph_load_cuad(write_fake_cuad())

  # The extractor returns a longer run of text than CUAD's excerpt for the
  # governing-law clause, and misses the liability cap entirely.
  use_fakes(populator = function(bundle, source, llm_fn, tier) list(
    entities = list(list(instance_id = "c1", type_id = "Clause", confidence = 0.9,
      source_refs = list(list(source_label = "d", excerpt = "e")),
      properties = list(clause_number = "1", clause_type = "governing_law",
        text = "This Agreement is governed by the laws of Ireland. Jurisdiction is Dublin."))),
    relationships = list(), amendments = list()))

  result <- orph_benchmark_extraction(con, cuad, tier = "local", limit = 1L,
                                      actor_id = "act_test", storage_root = root)

  expect_equal(result$n_contracts, 1)
  gl <- result$by_category[result$by_category$category == "Governing Law", ]
  expect_equal(gl$n_found, 1)     # containment counts, not string equality
  expect_equal(gl$recall, 1)

  cap <- result$by_category[result$by_category$category == "Cap On Liability", ]
  expect_equal(cap$n_found, 0)
  expect_equal(cap$recall, 0)

  expect_equal(result$overall_recall, 0.5)
})

test_that("unmapped categories are reported, never scored as misses", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  cuad <- orph_load_cuad(write_fake_cuad(include_absent = FALSE))
  use_fakes()

  # A mapping that knows nothing about Cap On Liability must not report it as
  # a failure of extraction -- that would be blaming the extractor for a gap in
  # the benchmark's own configuration.
  partial <- c("Governing Law" = "governing_law")
  result <- orph_benchmark_extraction(con, cuad, tier = "local", limit = 1L,
                                      actor_id = "act_test", storage_root = root,
                                      mapping = partial)

  expect_true("Cap On Liability" %in% result$unmapped_categories)
  expect_false("Cap On Liability" %in% result$by_category$category)
  expect_true(any(grepl("no clause_type mapping", result$caveats)))
})

test_that("the report states what the benchmark cannot tell you", {
  con <- new_test_store(); root <- test_storage_root(); seed_actors(con)
  cuad <- orph_load_cuad(write_fake_cuad()); use_fakes()
  result <- orph_benchmark_extraction(con, cuad, tier = "local", limit = 1L,
                                      actor_id = "act_test", storage_root = root)

  expect_true(any(grepl("Recall only", result$caveats)))
  # The most important caveat: a good CUAD score is not evidence about Irish
  # public-sector documents.
  expect_true(any(grepl("US commercial contracts", result$caveats)))
})
