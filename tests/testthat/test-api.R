# The router is constructed and its handlers invoked directly. That covers the
# auth filter, permission checks and error mapping without binding a port.

skip_if_no_plumber <- function() testthat::skip_if_not_installed("plumber")

new_test_api <- function(env = parent.frame()) {
  skip_if_no_plumber()
  path <- tempfile(fileext = ".sqlite")
  root <- file.path(tempdir(), paste0("api-store-", as.integer(runif(1, 1, 1e9))))
  pr <- orph_api(db_path = path, storage_root = root)
  con <- orpheus:::api_state$con
  withr::defer({
    try(orph_disconnect(con), silent = TRUE)
    unlink(paste0(path, c("", "-wal", "-shm", ".writer.lock")))
    unlink(root, recursive = TRUE)
  }, envir = env)
  list(pr = pr, con = con, root = root)
}

#' Invoke a route's handler the way plumber would
call_route <- function(api, verb, path, token = NULL, body = NULL, params = list()) {
  endpoint <- NULL
  for (group in api$pr$endpoints) {
    for (ep in group) {
      if (identical(ep$path, path) && verb %in% ep$verbs) endpoint <- ep
    }
  }
  if (is.null(endpoint)) stop("No route ", verb, " ", path)

  req <- new.env()
  req$REQUEST_METHOD <- verb
  req$HTTP_AUTHORIZATION <- if (is.null(token)) "" else paste("Bearer", token)
  req$postBody <- if (is.null(body)) "" else jsonlite::toJSON(body, auto_unbox = TRUE)
  req$args <- params
  req$argsQuery <- list()
  req$actor <- if (!is.null(token)) orph_authenticate(api$con, token) else NULL
  res <- new.env(); res$status <- 200L

  # plumber's exec() takes only (req, res) and reads path parameters out of
  # req$args, so params are set there rather than passed as arguments.
  result <- endpoint$exec(req = req, res = res)
  list(status = res$status, body = result)
}

test_that("the router exposes the pipeline's endpoints", {
  api <- new_test_api()
  paths <- unlist(lapply(api$pr$endpoints, function(g) vapply(g, function(e) e$path, character(1))))
  for (p in c("/health", "/documents", "/documents/<id>/extract", "/documents/<id>/classify",
              "/instances/<id>/amend", "/schema-amendments", "/documents/<id>/analyse",
              "/documents/<id>/corpus-analysis", "/audit/llm", "/admin/settings")) {
    expect_true(p %in% paths, info = p)
  }
})

test_that("health needs no token; everything else does", {
  api <- new_test_api()
  expect_equal(call_route(api, "GET", "/health")$body$status, "ok")
  expect_equal(call_route(api, "GET", "/documents")$status, 401L)
  expect_equal(call_route(api, "GET", "/capabilities")$status, 401L)
})

test_that("an authenticated actor can drive a document through the pipeline", {
  api <- new_test_api(); use_fakes(llm_json = paste0(
    '{"doc_type":"contract","sector":"health","jurisdiction":"Ireland",',
    '"confidence":0.9,"rationale":"Titled agreement."}'))
  owner <- orph_create_actor(api$con, "Owner")
  token <- orph_create_token(api$con, owner, "test")$token

  path <- write_contract_file()
  created <- call_route(api, "POST", "/documents", token, body = list(path = path))
  expect_equal(created$status, 201L)
  doc <- created$body$document_id

  classified <- call_route(api, "POST", "/documents/<id>/classify", token, params = list(id = doc))
  expect_equal(classified$body$doc_type, "contract")
  expect_equal(classified$body$status, "unconfirmed")

  extracted <- call_route(api, "POST", "/documents/<id>/extract", token,
                          body = list(tier = "local"), params = list(id = doc))
  expect_gt(extracted$body$n_entities, 0)
  expect_gt(extracted$body$n_deterministic, 0)

  instances <- call_route(api, "GET", "/documents/<id>/instances", token, params = list(id = doc))
  expect_gt(nrow(instances$body), 0)
})

test_that("re-uploading identical content returns the existing document, not a new one", {
  api <- new_test_api(); use_fakes()
  owner <- orph_create_actor(api$con, "Owner")
  token <- orph_create_token(api$con, owner, "test")$token
  path <- write_contract_file()

  first <- call_route(api, "POST", "/documents", token, body = list(path = path))
  second <- call_route(api, "POST", "/documents", token, body = list(path = path))
  expect_equal(first$status, 201L)
  expect_equal(second$status, 200L)
  expect_true(second$body$duplicate)
  expect_equal(second$body$document_id, first$body$document_id)
})

test_that("a stranger gets 403 on someone else's document", {
  api <- new_test_api(); use_fakes()
  owner <- orph_create_actor(api$con, "Owner")
  other <- orph_create_actor(api$con, "Other")
  owner_token <- orph_create_token(api$con, owner, "t")$token
  other_token <- orph_create_token(api$con, other, "t")$token

  doc <- call_route(api, "POST", "/documents", owner_token,
                    body = list(path = write_contract_file()))$body$document_id

  expect_equal(call_route(api, "GET", "/documents/<id>", other_token, params = list(id = doc))$status, 403L)
  expect_equal(call_route(api, "POST", "/documents/<id>/extract", other_token,
                          body = list(tier = "local"), params = list(id = doc))$status, 403L)
})

test_that("cloud extraction over the API needs both policy and opt-in", {
  api <- new_test_api(); use_fakes()
  owner <- orph_create_actor(api$con, "Owner")
  token <- orph_create_token(api$con, owner, "t")$token
  doc <- call_route(api, "POST", "/documents", token,
                    body = list(path = write_contract_file()))$body$document_id

  blocked <- call_route(api, "POST", "/documents/<id>/extract", token,
                        body = list(tier = "cloud", cloud_opt_in = TRUE), params = list(id = doc))
  expect_equal(blocked$status, 400L)
  expect_match(blocked$body$error$message, "disabled for this deployment")

  orph_set_setting(api$con, "cloud_ai_policy", "per_user")
  still_blocked <- call_route(api, "POST", "/documents/<id>/extract", token,
                              body = list(tier = "cloud"), params = list(id = doc))
  expect_match(still_blocked$body$error$message, "explicit per-request opt-in")
})

test_that("administrator-only routes are closed to ordinary actors", {
  api <- new_test_api()
  user  <- orph_create_actor(api$con, "User")
  admin <- orph_create_actor(api$con, "Admin", is_admin = TRUE)
  user_token  <- orph_create_token(api$con, user, "t")$token
  admin_token <- orph_create_token(api$con, admin, "t")$token

  expect_equal(call_route(api, "GET", "/audit/llm", user_token)$status, 403L)
  expect_equal(call_route(api, "POST", "/admin/settings", user_token,
                          body = list(key = "cloud_ai_policy", value = "org_allow"))$status, 403L)
  expect_equal(call_route(api, "POST", "/schema-amendments/<id>/review", user_token,
                          body = list(decision = "accepted"), params = list(id = "x"))$status, 403L)

  ok <- call_route(api, "POST", "/admin/settings", admin_token,
                   body = list(key = "cloud_ai_policy", value = "org_allow"))
  expect_equal(ok$body$value, "org_allow")
  expect_equal(orph_setting(api$con, "cloud_ai_policy"), "org_allow")
})

test_that("an invalid policy value is rejected rather than stored", {
  api <- new_test_api()
  admin <- orph_create_actor(api$con, "Admin", is_admin = TRUE)
  token <- orph_create_token(api$con, admin, "t")$token
  bad <- call_route(api, "POST", "/admin/settings", token,
                    body = list(key = "cloud_ai_policy", value = "anything"))
  expect_equal(bad$status, 400L)
  expect_equal(orph_setting(api$con, "cloud_ai_policy", "disabled"), "disabled")
})

test_that("amend requires a non-empty changes object", {
  api <- new_test_api(); use_fakes()
  owner <- orph_create_actor(api$con, "Owner")
  token <- orph_create_token(api$con, owner, "t")$token
  doc <- call_route(api, "POST", "/documents", token,
                    body = list(path = write_contract_file()))$body$document_id
  call_route(api, "POST", "/documents/<id>/extract", token,
             body = list(tier = "local"), params = list(id = doc))
  id <- DBI::dbGetQuery(api$con, "SELECT instance_id FROM instances_Company")$instance_id

  empty <- call_route(api, "POST", "/instances/<id>/amend", token,
                      body = list(note = "no changes"), params = list(id = id))
  expect_equal(empty$status, 400L)

  ok <- call_route(api, "POST", "/instances/<id>/amend", token,
                   body = list(changes = list(role = "prime_supplier")), params = list(id = id))
  expect_equal(ok$body$status, "amended")
})

test_that("sharing over the API grants the access the permission model describes", {
  api <- new_test_api(); use_fakes()
  owner <- orph_create_actor(api$con, "Owner")
  other <- orph_create_actor(api$con, "Other")
  owner_token <- orph_create_token(api$con, owner, "t")$token
  other_token <- orph_create_token(api$con, other, "t")$token
  doc <- call_route(api, "POST", "/documents", owner_token,
                    body = list(path = write_contract_file()))$body$document_id

  expect_equal(call_route(api, "GET", "/documents/<id>", other_token, params = list(id = doc))$status, 403L)
  call_route(api, "POST", "/documents/<id>/share", owner_token,
             body = list(actor_id = other, role = "viewer"), params = list(id = doc))
  expect_equal(call_route(api, "GET", "/documents/<id>", other_token, params = list(id = doc))$status, 200L)

  # A viewer still cannot write.
  expect_equal(call_route(api, "POST", "/documents/<id>/classify", other_token,
                          params = list(id = doc))$status, 403L)
})

test_that("an upload with no file is a 400 with a usable message", {
  api <- new_test_api()
  owner <- orph_create_actor(api$con, "Owner")
  token <- orph_create_token(api$con, owner, "t")$token
  res <- call_route(api, "POST", "/documents", token, body = list(nothing = "here"))
  expect_equal(res$status, 400L)
  expect_match(res$body$error$detail, "multipart")
})

test_that("corpus-wide quality is administrator only, per-document is not", {
  api <- new_test_api(); use_fakes()
  user  <- orph_create_actor(api$con, "User")
  admin <- orph_create_actor(api$con, "Admin", is_admin = TRUE)
  user_token  <- orph_create_token(api$con, user, "t")$token
  admin_token <- orph_create_token(api$con, admin, "t")$token

  doc <- call_route(api, "POST", "/documents", user_token,
                    body = list(path = write_contract_file()))$body$document_id
  call_route(api, "POST", "/documents/<id>/extract", user_token,
             body = list(tier = "local"), params = list(id = doc))

  denied <- call_route(api, "GET", "/quality", user_token)
  expect_equal(denied$status, 403L)
  expect_match(denied$body$error$detail, "documents/<id>/quality")

  allowed <- call_route(api, "GET", "/quality", admin_token)
  expect_equal(allowed$status, 200L)
  expect_equal(allowed$body$readiness$state, "unmeasured")

  # The owner can measure their own document without being an administrator.
  scoped <- call_route(api, "GET", "/documents/<id>/quality", user_token, params = list(id = doc))
  expect_equal(scoped$status, 200L)
  expect_equal(scoped$body$scope, doc)
})

test_that("a stranger cannot read a document's quality report", {
  api <- new_test_api(); use_fakes()
  owner <- orph_create_actor(api$con, "Owner")
  other <- orph_create_actor(api$con, "Other")
  owner_token <- orph_create_token(api$con, owner, "t")$token
  other_token <- orph_create_token(api$con, other, "t")$token
  doc <- call_route(api, "POST", "/documents", owner_token,
                    body = list(path = write_contract_file()))$body$document_id

  expect_equal(call_route(api, "GET", "/documents/<id>/quality", other_token,
                          params = list(id = doc))$status, 403L)
})
