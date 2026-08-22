# ---------------------------------------------------------------------------
# Datasette configuration.
#
# Datasette is a read-only client here. It never writes: the Plumber API is the
# single writer. Note that --immutable does NOT enforce that -- it makes SQLite
# skip the WAL, so a live store reads as empty. Read-only is enforced by mounting
# the file read-only, not by a Datasette flag. See docs/deployment.md.
#
# Row-level, per-document permissions are not something Datasette core can do
# from a metadata `allow` block -- those gate a whole table or database. The
# per-document rule needs a plugin implementing the permission_resources_sql
# hook (datasette-acl, or the pattern datasette-paper uses). The SQL that hook
# needs is generated from orph_permission_sql(), so the API and Datasette
# enforce one rule written once rather than two copies that drift.
# ---------------------------------------------------------------------------

#' @keywords internal
comment_block <- function(text) paste0("# ", gsub("\n", "\n# ", text))

#' Write the Datasette configuration file for the store
#'
#' One file rather than several: Datasette takes a single `--config`, and the
#' table descriptions, the `allow` blocks, the canned queries and the UI
#' plugin's settings all have to agree with the bundle and the permission model.
#' Generating them together is what stops them drifting apart.
#'
#' The API token is not written here. The file names the environment variable
#' Datasette should read it from, so regenerating never commits a secret.
#'
#' @param path Where to write the config YAML. The metadata file is written
#'   beside it as `metadata.yml` unless `metadata_path` says otherwise.
#' @param database_name The name Datasette will serve the database under.
#' @param bundle The bundle whose instance tables the generated queries span.
#'   Defaults to the shipped one.
#' @param api_url Base URL of the Plumber API the UI plugin should call.
#' @param max_file_size Per-file ceiling on browser uploads, in bytes.
#' @param metadata_path Where to write the descriptive metadata YAML.
#' @return Invisibly, a named character vector of both paths.
#' @export
orph_write_datasette_config <- function(path = "inst/datasette/datasette.yml",
                                        database_name = "orpheus",
                                        bundle = orph_load_bundle(),
                                        api_url = "http://127.0.0.1:8000",
                                        max_file_size = 50 * 1024 * 1024,
                                        metadata_path = file.path(dirname(path),
                                                                  "metadata.yml")) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  dir.create(dirname(metadata_path), recursive = TRUE, showWarnings = FALSE)
  db_hint <- paste0("data/", database_name, ".sqlite")
  basename_hint <- basename(path)
  metadata_basename <- basename(metadata_path)

  # Built from the bundle, not written out by hand. A canned query naming the
  # instance tables of one domain would quietly return partial answers the
  # moment a bundle added or renamed a type -- and the query it feeds is the
  # one reporting whether extraction is any good.
  instance_tables <- vapply(managed_object_types(bundle),
                            function(ot) ot$table_name, character(1))
  instance_union <- paste(
    sprintf("            %sSELECT instance_id, status FROM %s",
            c("", rep("UNION ALL ", max(0, length(instance_tables) - 1))),
            instance_tables),
    collapse = "\n")

  # Quoted, and quotes stripped from the name: the YAML here is built by
  # concatenation, and a bundle called "Contracts: core" would otherwise emit a
  # file Datasette cannot parse.
  title <- gsub('"', "", paste("Orpheus:", bundle$bundle_name %||% "document intelligence"))

  # Two files, because Datasette reads them through two different paths.
  # `--metadata` is descriptive text and is the only one that reaches the
  # rendered pages; `--config` is anything that changes behaviour. Putting a
  # canned query in the metadata file makes Datasette 1.0 fail at startup with
  # a KeyError on `sql`, and putting a description in the config file renders
  # nothing at all. Both are generated here so they cannot disagree.
  metadata_yaml <- paste0(
'# Descriptive metadata for the Orpheus store. Generated -- regenerate with
# orph_write_datasette_config() rather than editing.

title: "', title, '"
description_html: |-
  <p>Read-only view of the Orpheus store. Every write -- extraction, amendment,
  concept evaluation -- goes through the Plumber API, which is the single
  writer. This process only reads.</p>
  <p>AI-sourced rows carry <code>source</code>, <code>confidence</code> and
  <code>status</code>. A row with <code>status = unconfirmed</code> has not been
  checked by a person. <code>confidence</code> is one of five rubric levels, not
  an arbitrary score: 1.0 explicit, 0.9 clearly named, 0.7 implied, 0.5
  inferred, 0.2 speculative.</p>

databases:
  ', database_name, ':
    tables:
      llm_calls:
        description: Audit log of every model call, local and cloud.
      documents:
        description: >-
          One row per ingested document. review_status is the document-level
          flag; per-instance review state lives on the instance tables.
      edit_history:
        description: >-
          Append-only audit trail. Ordered by seq rather than timestamp --
          changes made in one transaction share a timestamp to the second.
      schema_amendments:
        description: >-
          Properties and types seen during population but not declared in the
          bundle. Accepting one changes the bundle for every document, so it is
          an administrator decision rather than an ordinary review.
      concept_evaluations:
        description: >-
          Document and database-scope analysis. stale = 1 means an instance it
          depended on has since been amended. resolution_quality =
          naive_unresolved means matching was on raw name text, not resolved
          entities.
      provenance:
        description: >-
          Where each instance came from -- source label, page, and the excerpt
          supporting it.
')

  yaml <- paste0(
'# Datasette configuration for the Orpheus store. Generated -- regenerate with
# orph_write_datasette_config() rather than editing.
#
#   ORPHEUS_API_TOKEN=... datasette serve ', db_hint, ' \\
#     --metadata ', metadata_basename, ' --config ', basename_hint, ' \\
#     --plugins-dir plugins --template-dir templates --port 8001

# No anonymous access. The documents this store holds have no public audience,
# so an actor is required before any resource-level rule is even consulted.
allow:
  id: "*"

# The UI plugin. It is a client over the HTTP API -- it opens no SQLite
# connection and calls no model, so the single-writer lock and the cloud opt-in
# gate both still hold when a person is driving. The token is read from the
# environment so that regenerating this file never commits a secret.
plugins:
  orpheus-datasette:
    api_url: "', api_url, '"
    max_file_size: ', format(max_file_size, scientific = FALSE), '
    token:
      $env: ORPHEUS_API_TOKEN

databases:
  ', database_name, ':
    tables:
      actor_tokens:
        allow: false
      actors:
        allow:
          is_admin: 1
      llm_calls:
        allow:
          is_admin: 1
      edit_history:
        sort: seq

    queries:
      needs_review:
        title: Documents still needing review
        sql: |-
          SELECT d.document_id, d.filename, d.doc_type, d.review_status,
                 COUNT(ii.instance_id) AS instances
          FROM documents d
          LEFT JOIN instance_index ii ON ii.document_id = d.document_id
          WHERE d.review_status = \'unreviewed\'
          GROUP BY d.document_id
          ORDER BY d.date_added DESC

      low_confidence_unconfirmed:
        title: Unconfirmed findings at or below the inferred rubric level
        sql: |-
          SELECT i.type_id, i.instance_id, i.document_id, p.excerpt, p.confidence
          FROM instance_index i
          JOIN provenance p ON p.instance_id = i.instance_id
          WHERE p.confidence <= 0.5
          ORDER BY p.confidence ASC, i.created_at DESC

      stale_evaluations:
        title: Analyses invalidated by a later amendment
        sql: |-
          SELECT evaluation_id, concept_id, kind, target_document_id,
                 stale_reason, generated_at
          FROM concept_evaluations
          WHERE stale = 1
          ORDER BY generated_at DESC

      cloud_calls:
        title: What has been sent to the cloud model
        sql: |-
          SELECT c.created_at, c.purpose, c.document_id, d.filename,
                 c.actor_id, c.prompt_chars, c.excerpt_only, c.model
          FROM llm_calls c
          LEFT JOIN documents d ON d.document_id = c.document_id
          WHERE c.tier = \'cloud\'
          ORDER BY c.created_at DESC
        allow:
          is_admin: 1

      extraction_accuracy_by_confidence:
        title: Does the confidence rubric actually rank reliability?
        sql: |-
          -- Joins through provenance, which is what keeps rule-raised flags out:
          -- a concept flag has no provenance row, because it is not an
          -- extraction. Give concept flags provenance and this query starts
          -- reporting rule precision as extraction accuracy.
          SELECT p.confidence,
                 COUNT(*) AS reviewed,
                 SUM(CASE WHEN x.status = \'confirmed\' THEN 1 ELSE 0 END) AS confirmed,
                 SUM(CASE WHEN x.status = \'amended\'   THEN 1 ELSE 0 END) AS amended,
                 SUM(CASE WHEN x.status = \'rejected\'  THEN 1 ELSE 0 END) AS rejected,
                 ROUND(1.0 * SUM(CASE WHEN x.status = \'confirmed\' THEN 1 ELSE 0 END)
                       / COUNT(*), 3) AS accuracy
          FROM provenance p
          JOIN (
', instance_union, '
          ) x ON x.instance_id = p.instance_id
          WHERE x.status IN (\'confirmed\', \'amended\', \'rejected\')
          GROUP BY p.confidence
          ORDER BY p.confidence DESC

      rule_concept_precision:
        title: How often does each rule concept point at something real?
        sql: |-
          SELECT flag_type AS concept_id,
                 COUNT(*) AS raised,
                 SUM(CASE WHEN status IN (\'confirmed\', \'amended\') THEN 1 ELSE 0 END) AS upheld,
                 SUM(CASE WHEN status = \'rejected\' THEN 1 ELSE 0 END) AS dismissed,
                 SUM(CASE WHEN status = \'unconfirmed\' THEN 1 ELSE 0 END) AS unreviewed
          FROM instances_Flag
          WHERE raised_by_pass = \'concept\'
          GROUP BY flag_type
          ORDER BY dismissed DESC

      risk_score_vs_model:
        title: Where the rule score and the model\'s reading disagree
        sql: |-
          SELECT s.target_document_id AS document_id,
                 d.filename,
                 json_extract(s.result, \'$.tier\')        AS score_tier,
                 json_extract(s.result, \'$.score\')       AS score,
                 json_extract(n.result, \'$.risk_level\')  AS model_level
          FROM concept_evaluations s
          JOIN concept_evaluations n
            ON n.target_document_id = s.target_document_id
           AND n.kind = \'narrative\' AND n.stale = 0
          LEFT JOIN documents d ON d.document_id = s.target_document_id
          WHERE s.kind = \'score\' AND s.stale = 0
            AND LOWER(json_extract(s.result, \'$.tier\'))
                != LOWER(json_extract(n.result, \'$.risk_level\'))
          ORDER BY score DESC

      amendment_trail:
        title: Every human correction, newest first
        sql: |-
          SELECT seq, edited_at, edited_by, table_name, row_id, action,
                 previous_value, new_value, note
          FROM edit_history
          WHERE action IN (\'amend\', \'confirm\', \'reject\')
          ORDER BY seq DESC

# ---------------------------------------------------------------------------
# Per-document row-level permissions
#
# Datasette core `allow` blocks gate a table or a database, not rows. The rules
# below are what a permission_resources_sql plugin hook should run. They are
# generated from orph_permission_sql(), so they cannot drift from what the API
# enforces. Bind :actor_id to the authenticated actor.
#
# Regenerate this file with orph_write_datasette_config() after any change to
# the permission model.
# ---------------------------------------------------------------------------
', comment_block(orph_permission_sql("view")), '
#
', comment_block(orph_permission_sql("edit")), '
')

  writeLines(metadata_yaml, metadata_path)
  writeLines(yaml, path)
  invisible(c(config = path, metadata = metadata_path))
}

#' The command to serve the store to readers
#'
#' Deliberately **not** `--immutable`, despite that being the obvious choice for
#' a database this process never writes to.
#'
#' `--immutable` sets SQLite's `immutable=1`, which tells SQLite the file cannot
#' change and lets it skip the write-ahead log. With WAL enabled -- which this
#' store requires, so readers are not blocked by the writer -- committed data
#' lives in the `-wal` sidecar until a checkpoint. An immutable reader therefore
#' sees the database as of the last checkpoint, and on a live store that means
#' silently missing or stale rows, with no error to notice.
#'
#' Serving read-only without `--immutable` reads the WAL and sees current data.
#' Datasette core never writes to an attached database, and the single-writer
#' lock stops a second writer regardless, so nothing is given up by dropping the
#' flag.
#'
#' @param db_path Path to the SQLite file.
#' @param metadata_path Path to the descriptive metadata YAML.
#' @param config_path Path to the configuration YAML.
#' @param port Port to bind.
#' @param ui Include the UI plugin's flags.
#' @param immutable Use `--immutable` anyway. Only correct for a snapshot that
#'   has been checkpointed and is no longer being written to -- see
#'   [orph_checkpoint()].
#' @return A character scalar.
#' @export
orph_datasette_command <- function(db_path = "data/orpheus.sqlite",
                                   metadata_path = "inst/datasette/metadata.yml",
                                   config_path = "inst/datasette/datasette.yml",
                                   port = 8001, ui = TRUE, immutable = FALSE) {
  sprintf(paste("datasette serve%s %s --metadata %s --config %s%s --port %d",
                "--setting sql_time_limit_ms 3000 --setting max_returned_rows 2000"),
          if (immutable) " --immutable" else "", db_path, metadata_path,
          config_path,
          if (ui) " --plugins-dir plugins --template-dir templates" else "",
          port)
}
