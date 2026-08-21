test_that("confidence snaps onto the rubric and never rounds upward", {
  expect_equal(orph_snap_confidence(1.0), 1.0)
  expect_equal(orph_snap_confidence(0.9), 0.9)
  # 0.8 is not a rubric level; it must land on 0.7, not be promoted to 0.9.
  expect_equal(orph_snap_confidence(0.8), 0.7)
  expect_equal(orph_snap_confidence(0.69), 0.5)
  expect_equal(orph_snap_confidence(0.01), 0.2)
  expect_equal(orph_snap_confidence(-5), 0.2)
  expect_equal(orph_snap_confidence(99), 1.0)
})

test_that("a missing confidence is treated as inferred, not as certain", {
  expect_equal(orph_snap_confidence(NA), 0.5)
  expect_equal(orph_snap_confidence(NULL), numeric(0))
})

test_that("every rubric level has a label", {
  expect_equal(orph_confidence_label(c(1.0, 0.9, 0.7, 0.5, 0.2)),
               c("explicit", "named", "implied", "inferred", "speculative"))
  expect_equal(orph_confidence_label(0.83), "unknown")
})

test_that("naive keys strip the noise that varies between documents", {
  expect_equal(orph_naive_key("Meridian Systems Limited"), "meridian systems")
  expect_equal(orph_naive_key("MERIDIAN SYSTEMS LTD."),    "meridian systems")
  expect_equal(orph_naive_key("Meridian Systems plc"),     "meridian systems")
})

test_that("naive keys have the limits the corpus analysis warns about", {
  # This is the documented failure mode, asserted so it cannot change silently:
  # an ampersand and the word "and" produce different keys.
  expect_false(identical(orph_naive_key("Ernst & Young"), orph_naive_key("Ernst and Young")))
  # And two genuinely different bodies sharing a name collapse together.
  expect_identical(orph_naive_key("Apex Ltd"), orph_naive_key("APEX LIMITED"))
})
