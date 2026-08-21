# Shared fixtures.
#
# Every test runs against a real SQLite store with the real bundle and the real
# schema. Only the model and the population engine are doubled, because those
# are the two things that need a network and a GPU; everything else -- WAL,
# transactions, the permission rules, conceptR's SQL evaluation -- is exercised
# as it actually runs.

skip_if_no_conceptr <- function() {
  testthat::skip_if_not_installed("conceptR")
}

#' A store with migrations applied and the shipped bundle registered
new_test_store <- function(env = parent.frame()) {
  path <- tempfile(fileext = ".sqlite")
  con <- orph_init_store(path)
  withr::defer({
    # Providers are package-level state. A fake left installed by one test
    # would silently serve the next one, so they are cleared with the store
    # rather than only where a test remembers to.
    orph_set_populator(NULL)
    orph_set_llm_provider("local", NULL)
    orph_set_llm_provider("cloud", NULL)
    try(orph_disconnect(con), silent = TRUE)
    unlink(paste0(path, c("", "-wal", "-shm", ".writer.lock")))
  }, envir = env)
  con
}

test_storage_root <- function(env = parent.frame()) {
  root <- file.path(tempdir(), paste0("orph-store-", as.integer(runif(1, 1, 1e9))))
  withr::defer(unlink(root, recursive = TRUE), envir = env)
  root
}

#' Write a small contract-like text file
write_contract_file <- function(name = "services-agreement.txt",
                                supplier = "Meridian Systems Limited",
                                value = "EUR 2,400,000",
                                extra = character()) {
  path <- file.path(tempdir(), paste0(as.integer(runif(1, 1, 1e9)), "-", name))
  # The reference line carries the file name so two fixtures never have byte
  # identical content -- ingest dedups on content hash, and a fixture that
  # collapses into an earlier document makes for a confusing test failure.
  writeLines(c(
    "SERVICES AGREEMENT",
    paste0("Document: ", name),
    paste0("Between the Department of Health and ", supplier, "."),
    paste0("Contract reference DOH-2024-0117. Total value ", value, "."),
    "Commencing on 1 January 2024 and shall expire on 2026-12-31.",
    "12. LIABILITY",
    "The Supplier shall indemnify the Authority without limitation.",
    extra), path)
  path
}

#' A model that always returns the given JSON
fake_llm <- function(json) {
  function(system_prompt) list(chat = function(user_prompt) json)
}

#' A population engine returning a fixed, schema-valid result
fake_populator <- function(contract_name = "Services Agreement",
                           supplier = "Meridian Systems Limited",
                           value = 2400000,
                           extra_properties = list(),
                           extra_entities = list(),
                           relationships = NULL) {
  function(bundle, source, llm_fn, tier) {
    entities <- c(list(
      list(instance_id = "e1", type_id = "Contract", confidence = 0.9,
           source_refs = list(list(source_label = source$source_label,
                                   excerpt = "--- Page 1 ---\nSERVICES AGREEMENT")),
           properties = c(list(name = contract_name, reference = "DOH-2024-0117",
                               value_amount = value, value_currency = "EUR",
                               signature_block_present = "no"),
                          extra_properties)),
      list(instance_id = "e2", type_id = "Company", confidence = 0.9,
           source_refs = list(list(source_label = source$source_label, excerpt = supplier)),
           properties = list(name = supplier, role = "supplier")),
      list(instance_id = "e3", type_id = "Clause", confidence = 0.9,
           source_refs = list(list(source_label = source$source_label,
                                   excerpt = "12. LIABILITY")),
           properties = list(clause_number = "12", heading = "LIABILITY",
                             clause_type = "liability",
                             text = "The Supplier shall indemnify the Authority without limitation."))
    ), extra_entities)

    list(entities = entities,
         relationships = relationships %||% list(
           list(link_type_id = "party_to", from_instance_id = "e2",
                to_instance_id = "e1", confidence = 0.9, evidence = supplier)),
         amendments = list())
  }
}

`%||%` <- function(x, y) if (is.null(x)) y else x

#' Install doubles for the duration of a test
use_fakes <- function(populator = fake_populator(), llm_json = NULL,
                      env = parent.frame()) {
  old_pop <- orph_set_populator(populator)
  withr::defer(orph_set_populator(old_pop), envir = env)
  if (!is.null(llm_json)) {
    old_llm <- orph_set_llm_provider("local", fake_llm(llm_json))
    withr::defer(orph_set_llm_provider("local", old_llm), envir = env)
  }
  invisible(TRUE)
}

#' Ingest and extract in one step, returning the document id
seed_document <- function(con, storage_root, ...) {
  path <- write_contract_file(...)
  ing <- orph_ingest(con, path, actor_id = "act_test", storage_root = storage_root)
  orph_extract(con, ing$document_id, tier = "local", actor_id = "act_test")
  ing$document_id
}

#' Create the standard cast of actors
seed_actors <- function(con) {
  list(
    owner  = orph_create_actor(con, "Owner",  actor_id = "act_test"),
    other  = orph_create_actor(con, "Other",  actor_id = "act_other"),
    admin  = orph_create_actor(con, "Admin",  actor_id = "act_admin", is_admin = TRUE)
  )
}
