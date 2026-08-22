# ---------------------------------------------------------------------------
# Datasette configuration.
#
# Datasette is a read-only client here. It never writes: the Plumber API is the
# single writer, and running Datasette with --immutable makes that a property
# of the process rather than a rule people have to remember.
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

#' Write a Datasette metadata file for the store
#'
#' @param path Where to write the YAML.
#' @param database_name The name Datasette will serve the database under.
#' @param bundle The bundle whose instance tables the generated queries span.
#'   Defaults to the shipped one.
#' @return Invisibly, `path`.
#' @export
orph_write_datasette_metadata <- function(path = "inst/datasette/metadata.yml",
                                          database_name = "orpheus",
                                          bundle = orph_load_bundle()) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)

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

  yaml <- paste0(
'title: Orpheus contract intelligence
description_html: |-
  <p>Read-only view of the Orpheus store. Every write -- extraction, amendment,
  concept evaluation -- goes through the Plumber API, which is the single
  writer. This instance runs with <code>--immutable</code> and cannot change
  anything.</p>
  <p>AI-sourced rows carry <code>source</code>, <code>confidence</code> and
  <code>status</code>. A row with <code>status = unconfirmed</code> has not been
  checked by a person. <code>confidence</code> is one of five rubric levels, not
  an arbitrary score: 1.0 explicit, 0.9 clearly named, 0.7 implied, 0.5
  inferred, 0.2 speculative.</p>

# No anonymous access. Contract documents have no public audience, so an actor
# is required before any resource-level rule is even consulted.
allow:
  id: "*"

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
        description: Audit log of every model call, local and cloud.
      documents:
        description: >-
          One row per ingested document. review_status is the document-level
          flag; per-instance review state lives on the instance tables.
      edit_history:
        description: >-
          Append-only audit trail. Ordered by seq rather than timestamp --
          changes made in one transaction share a timestamp to the second.
        sort: seq
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
# Regenerate this file with orph_write_datasette_metadata() after any change to
# the permission model.
# ---------------------------------------------------------------------------
', comment_block(orph_permission_sql("view")), '
#
', comment_block(orph_permission_sql("edit")), '
')

  writeLines(yaml, path)
  invisible(path)
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
#' @param metadata_path Path to the metadata YAML.
#' @param port Port to bind.
#' @param immutable Use `--immutable` anyway. Only correct for a snapshot that
#'   has been checkpointed and is no longer being written to -- see
#'   [orph_checkpoint()].
#' @return A character scalar.
#' @export
orph_datasette_command <- function(db_path = "data/orpheus.sqlite",
                                   metadata_path = "inst/datasette/metadata.yml",
                                   port = 8001, immutable = FALSE) {
  sprintf(paste("datasette serve%s %s --metadata %s --port %d",
                "--setting sql_time_limit_ms 3000 --setting max_returned_rows 2000"),
          if (immutable) " --immutable" else "", db_path, metadata_path, port)
}

#' Note explaining the database-name requirement
#'
#' Datasette names a database after its filename stem, and metadata keys are
#' matched against that name. Serving `contracts.sqlite` against metadata
#' declaring a database called `orpheus` silently drops every canned query and
#' table description -- the pages still load, they just lose their
#' configuration. The file must be named to match.
#'
#' @param database_name The name used in the metadata file.
#' @return A character scalar.
#' @export
orph_datasette_naming_note <- function(database_name = "orpheus") {
  sprintf(paste0(
    "The SQLite file must be named %s.sqlite: Datasette derives the database ",
    "name from the filename stem and matches metadata keys against it. A ",
    "mismatch drops the canned queries and table descriptions without an error."),
    database_name)
}
