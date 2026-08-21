# Entry point for `plumber::plumb()` / `Rscript inst/plumber/plumb.R`.
#
# This process is the single writer for the store. Do not run a second copy
# against the same database file -- it will refuse to start, by design.

library(orpheus)

db_path      <- Sys.getenv("ORPHEUS_DB", "data/orpheus.sqlite")
storage_root <- Sys.getenv("ORPHEUS_STORAGE", "storage")
host         <- Sys.getenv("ORPHEUS_HOST", "127.0.0.1")
port         <- as.integer(Sys.getenv("ORPHEUS_PORT", "8000"))

pr <- orph_api(db_path = db_path, storage_root = storage_root,
               force_lock = identical(Sys.getenv("ORPHEUS_FORCE_LOCK"), "1"))

if (identical(environment(), globalenv()) && !interactive()) {
  # Bind to localhost by default. Terminating TLS is the reverse proxy's job:
  # this service must not be exposed directly, and the deployment notes in
  # docs/deployment.md say so.
  pr$run(host = host, port = port)
}
