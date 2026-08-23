"""`orpheus` on the command line.

Two audiences, and they want opposite things. Someone standing up a deployment
wants `init`, `serve` and `token` — a few commands that leave a working system
behind. Someone measuring extraction quality wants to run the pipeline over a
directory and read a report, without a browser in the way.

Both are the same core the plugin calls, so anything done here is visible there
and carries the same provenance. What is deliberately *not* here is a second way
to write: every command opens the store the ordinary way, takes the advisory
writer lock, and releases it. Running a command against a live Datasette is
therefore refused by the lock rather than silently corrupting a WAL — which is
the failure the lock exists to make loud.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import analysis, auth, bundle as bundle_mod, classify, concepts
from . import datasette_config, extract as extract_mod, ingest as ingest_mod
from . import quality, review
from .store import Store
from .utils import OrpheusError

DEFAULT_DB = "data/orpheus.sqlite"
DOCUMENT_SUFFIXES = (".pdf", ".docx", ".txt", ".md",
                     ".png", ".jpg", ".jpeg", ".tif", ".tiff")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def emit(value, as_json: bool) -> None:
    if as_json:
        json.dump(value, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, default=str))


def open_store(args, mode: str = "write") -> Store:
    return Store(args.db, mode=mode, force_lock=getattr(args, "force_lock", False))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    """Create a store, load a bundle, and write the Datasette files."""
    store = open_store(args)
    try:
        bundle = bundle_mod.load(args.bundle) if args.bundle else bundle_mod.load()
        bundle_mod.register(store, bundle)
        bundle_mod.apply_schema(store, bundle)
        concepts.setup_concepts(store, bundle)
        concepts.setup_scores(store, bundle)

        actor_id = None
        if args.admin:
            actor_id = auth.create_actor(store, args.admin, email=args.admin_email,
                                         is_admin=True)
        if args.cloud_policy:
            store.set_setting("cloud_ai_policy", args.cloud_policy, actor_id)

        paths = datasette_config.write_config(
            args.config, database_name=Path(args.db).stem, bundle=bundle,
            storage_root=args.storage_root)
    finally:
        store.close()

    emit({"database": args.db, "bundle": bundle["bundleId"],
          "bundle_version": bundle["bundleVersion"],
          "admin_actor_id": actor_id, **paths,
          "next": datasette_config.serve_command(
              args.db, paths["metadata"], paths["config"])}, args.json)
    return 0


def cmd_serve(args) -> int:
    """Print, or run, the command that serves this store."""
    command = datasette_config.serve_command(
        args.db, args.metadata, args.config, port=args.port, ui=not args.no_ui)
    if args.print_only:
        print(command)
        return 0
    import shlex
    import subprocess
    return subprocess.call(shlex.split(command))


def cmd_token(args) -> int:
    store = open_store(args)
    try:
        result = auth.create_token(store, args.actor_id, label=args.label)
    finally:
        store.close()
    # Printed once and never stored: the store keeps only the hash.
    emit(result, args.json)
    return 0


def cmd_actor(args) -> int:
    store = open_store(args)
    try:
        actor_id = auth.create_actor(store, args.name, email=args.email,
                                     is_admin=args.admin)
    finally:
        store.close()
    emit({"actor_id": actor_id, "display_name": args.name,
          "is_admin": args.admin}, args.json)
    return 0


def _documents_under(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*")
                  if p.is_file() and p.suffix.lower() in DOCUMENT_SUFFIXES)


def cmd_ingest(args) -> int:
    """Ingest one file or a directory, and optionally run the pipeline over it.

    A directory is how a corpus arrives, and a corpus is the only way to answer
    the question this project exists to answer. Each document is its own
    transaction: one unreadable PDF in a hundred should cost that document, not
    the run.
    """
    paths = _documents_under(Path(args.path))
    if not paths:
        raise OrpheusError(f"No documents under {args.path}.")

    store = open_store(args)
    results = []
    try:
        for path in paths:
            entry: dict = {"path": str(path)}
            try:
                document = ingest_mod.ingest(store, path, actor_id=args.actor_id,
                                             storage_root=args.storage_root,
                                             visibility=args.visibility)
                entry.update(document_id=document["document_id"],
                             pages=document.get("n_pages"),
                             duplicate=bool(document.get("duplicate")))
                if not args.extract or document.get("duplicate"):
                    results.append(entry)
                    continue
                # Classification is a convenience and it needs a model.
                # Letting it fail the document would mean a store with no
                # model configured could not ingest anything at all, when the
                # deterministic pass would have worked perfectly well. The
                # plugin makes the same call, so both surfaces agree.
                try:
                    entry["classification"] = classify.classify(
                        store, document["document_id"], actor_id=args.actor_id,
                        tier="local")
                except OrpheusError as exc:
                    entry["classification_error"] = str(exc)
                entry["extraction"] = extract_mod.extract(
                    store, document["document_id"], tier=args.tier,
                    actor_id=args.actor_id, opt_in=args.cloud_opt_in,
                    engine_name=args.engine)
                if args.concepts:
                    entry["concepts"] = concepts.evaluate_concepts(
                        store, document["document_id"], actor_id=args.actor_id)
            except OrpheusError as exc:
                entry["error"] = str(exc)
            results.append(entry)
            if not args.json:
                print(f"{path.name}: {_summarise(entry, args.extract)}", flush=True)
    finally:
        store.close()

    failed = [r for r in results if r.get("error")]
    if args.json:
        emit({"documents": results, "n_failed": len(failed)}, True)
    elif failed:
        print(f"\n{len(failed)} of {len(results)} failed.", file=sys.stderr)
    return 1 if failed and len(failed) == len(results) else 0


def _summarise(entry: dict, extracting: bool) -> str:
    """One line per document, saying what actually happened to it."""
    if entry.get("error"):
        return entry["error"]
    if entry.get("duplicate"):
        return "already ingested"
    if not extracting:
        return f"ingested, {entry.get('pages') or 0} page(s)"

    extraction = entry.get("extraction") or {}
    # The deterministic findings are counted separately from the model's, and
    # a line that reports only the model's says "0 found" for a run that found
    # four dates without one.
    found = extraction.get("n_entities", 0) + extraction.get("n_deterministic", 0)
    line = f"{found} found"
    if extraction.get("n_edges"):
        line += f", {extraction['n_edges']} link(s)"
    if extraction.get("model_error"):
        line += f" (deterministic only -- {extraction['model_error']})"
    if entry.get("classification_error"):
        line += " (unclassified)"
    return line


def cmd_extract(args) -> int:
    store = open_store(args)
    try:
        result = extract_mod.extract(store, args.document_id, tier=args.tier,
                                     actor_id=args.actor_id,
                                     opt_in=args.cloud_opt_in,
                                     force=args.force, engine_name=args.engine)
    finally:
        store.close()
    emit(result, args.json)
    # A partial run is not a failure worth stopping a script over, but it is not
    # a success either: the caller gets a distinct status rather than a message
    # they have to parse.
    return 2 if result.get("model_error") else 0


def cmd_analyse(args) -> int:
    store = open_store(args)
    try:
        result = analysis.corpus_analysis(store, args.document_id,
                                          actor_id=args.actor_id)
    finally:
        store.close()
    emit(result, args.json)
    return 0


def cmd_report(args) -> int:
    """The report Phase 1 exists to produce."""
    store = open_store(args, mode="read")
    try:
        report = quality.quality_report(store, document_id=args.document_id,
                                        min_reviewed=args.min_reviewed)
    finally:
        store.close()
    if args.json:
        emit(report, True)
        return 0

    print(report["headline"])
    overall = report["extraction"]["overall"]
    if overall.get("n_reviewed"):
        print(f"\n  reviewed   {overall['n_reviewed']} of {overall['n_total']}")
        print(f"  confirmed  {overall['n_confirmed']}")
        print(f"  amended    {overall['n_amended']}")
        print(f"  rejected   {overall['n_rejected']}")

    calibration = report["calibration"]
    print(f"\n  calibration: {calibration['verdict']}")
    for level in calibration.get("levels", []):
        accuracy = level.get("accuracy")
        measured = f"{accuracy:.0%} correct" if accuracy is not None \
            else "not enough reviewed"
        print(f"    {level['confidence_label']:<12} {level['confidence']}  "
              f"{level['n_reviewed']}/{level['n_total']} reviewed, {measured}")
    # An inversion is the finding that matters: a level the machine was more
    # sure about turning out to be less often right than one below it.
    for inversion in calibration.get("inversions", []):
        print(f"    ! {inversion}")

    # The finding a corpus run exists for: did the engine quote things the
    # document actually says?
    for entry in report["grounding"]["by_source"]:
        if not entry["n_quoted"]:
            continue
        print(f"\n  {entry['source']}: {entry['n_grounded']}/{entry['n_quoted']} "
              f"quotations located in the document "
              f"({entry['fabrication_rate']:.0%} not found)")
    print(f"  {report['grounding']['note']}")

    corrections = report["property_corrections"]
    if corrections:
        print("\n  most corrected:")
        for row in corrections[:5]:
            print(f"    {row['type_id']}.{row['property']}  {row['n']}")

    violations = report["codelist_violations"]
    if violations:
        print(f"\n  {len(violations)} value(s) outside a declared codelist")
    return 0


def cmd_review(args) -> int:
    store = open_store(args)
    try:
        if args.action == "confirm":
            review.confirm_instance(store, args.instance_id, args.actor_id,
                                    note=args.note)
        elif args.action == "reject":
            review.reject_instance(store, args.instance_id, args.actor_id,
                                   note=args.note)
        else:
            changes = dict(pair.split("=", 1) for pair in args.set)
            review.amend_instance(store, args.instance_id, changes,
                                  args.actor_id, note=args.note)
        progress = review.review_progress(
            store, review.locate_instance(store, args.instance_id)["document_id"])
    finally:
        store.close()
    emit({"instance_id": args.instance_id, "action": args.action, **progress},
         args.json)
    return 0


def cmd_search(args) -> int:
    """Search the corpus, or find where a name appears unextracted."""
    from . import search as search_mod

    if args.build:
        store = open_store(args)
        try:
            emit(search_mod.enable_search(store, rebuild=args.rebuild), args.json)
        finally:
            store.close()
        return 0

    if not args.query:
        raise OrpheusError("Give something to search for, or --build the index.")

    store = open_store(args, mode="read")
    try:
        if args.unlinked:
            result = search_mod.unlinked_mentions(store, args.query, limit=args.limit)
        else:
            result = {"pages": search_mod.search_pages(store, args.query, args.limit)}
    finally:
        store.close()

    if args.json:
        emit(result, True)
        return 0

    if args.unlinked:
        print(f"{args.query!r}  (key: {result['naive_key']})")
        print(f"\n  extracted in : {', '.join(result['linked_documents']) or 'nowhere'}")
        print(f"  mentioned in, but not extracted:")
        for hit in result["unlinked"] or []:
            print(f"    {hit['document_id']}  p{hit['page_no']}")
        if not result["unlinked"]:
            print("    (nothing)")
        print(f"\n  {result['caveat']}")
    else:
        for hit in result["pages"]:
            snippet = " ".join((hit.get("text") or "").split())[:90]
            print(f"  {hit['document_id']}  p{hit['page_no']}  {snippet}")
    return 0


def cmd_property(args) -> int:
    """Rename or drop a property on a type, table and bundle together."""
    from . import schema_ops

    store = open_store(args)
    try:
        if args.action == "rename":
            if not args.to:
                raise OrpheusError("Give --to for a rename.")
            result = schema_ops.rename_property(store, args.type_id, args.property_id,
                                                args.to, actor_id=args.actor_id)
        elif args.force:
            result = schema_ops.force_drop_property(store, args.type_id,
                                                    args.property_id,
                                                    actor_id=args.actor_id)
        else:
            result = schema_ops.drop_property(store, args.type_id, args.property_id,
                                              actor_id=args.actor_id)
    finally:
        store.close()
    emit(result, args.json)
    return 0


def cmd_config(args) -> int:
    """Regenerate the Datasette files from the bundle in the store."""
    store = open_store(args, mode="read")
    try:
        bundle = bundle_mod.active(store) or bundle_mod.load()
    finally:
        store.close()
    paths = datasette_config.write_config(args.config, bundle=bundle,
                                          database_name=Path(args.db).stem,
                                          storage_root=args.storage_root)
    emit(paths, args.json)
    return 0


def cmd_bundle(args) -> int:
    """Validate a bundle without touching a store."""
    schema_checked = bundle_mod.schema_validation_available()
    if args.strict and not schema_checked:
        raise OrpheusError(
            "--strict needs jsonschema, which is not installed, so only the "
            "semantic checks would run. `pip install jsonschema`."
        )
    bundle = bundle_mod.load(args.path) if args.path else bundle_mod.load()
    bundle_mod.validate(bundle)
    if not (args.json or schema_checked):
        print("note: jsonschema is not installed, so only the semantic checks "
              "ran. `pip install jsonschema` to check the shape too.",
              file=sys.stderr)
    emit({"bundle_id": bundle["bundleId"], "version": bundle["bundleVersion"],
          "valid": True, "schema_checked": schema_checked,
          "object_types": [o["id"] for o in
                           bundle_mod.managed_object_types(bundle)],
          "tables": [bundle_mod.table_name(o) for o in
                     bundle_mod.managed_object_types(bundle)]}, args.json)
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # The global flags are declared once and inherited by every subcommand, so
    # `orpheus --db x bundle` and `orpheus bundle --db x` both work. Argparse
    # only accepts a top-level flag *before* the subcommand, and that is not
    # where anyone types it.
    #
    # SUPPRESS rather than a default on each copy: a subparser parses into its
    # own namespace and copies every attribute back, so a plain default here
    # would have the subcommand quietly overwrite the value the top-level flag
    # just parsed. The defaults are filled in after parsing instead --
    # set_defaults() cannot do it, because it reaches into the shared action
    # objects and replaces the SUPPRESS this depends on.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="path to the store")
    common.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("--force-lock", action="store_true",
                        default=argparse.SUPPRESS,
                        help="take over a writer lock left by a dead process")

    parser = argparse.ArgumentParser(
        prog="orpheus", description=__doc__.split("\n")[0], parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name, fn, help_text):
        sub = subparsers.add_parser(name, help=help_text, parents=[common],
                                    description=fn.__doc__ or help_text)
        sub.set_defaults(func=fn)
        return sub

    init = add("init", cmd_init, "create a store and its Datasette files")
    init.add_argument("--bundle", help="bundle JSON (default: the shipped one)")
    init.add_argument("--admin", help="display name of a first admin actor")
    init.add_argument("--admin-email")
    init.add_argument("--cloud-policy",
                      choices=("disabled", "per_user", "org_allow"))
    init.add_argument("--config", default="config/datasette.yml")
    init.add_argument("--storage-root", default="storage")

    serve = add("serve", cmd_serve, "serve the store with Datasette")
    serve.add_argument("--metadata", default="config/metadata.yml")
    serve.add_argument("--config", default="config/datasette.yml")
    serve.add_argument("--port", type=int, default=8001)
    serve.add_argument("--no-ui", action="store_true",
                       help="omit the plugin and template directories")
    serve.add_argument("--print-only", action="store_true",
                       help="print the command instead of running it")

    actor = add("actor", cmd_actor, "create an actor")
    actor.add_argument("name")
    actor.add_argument("--email")
    actor.add_argument("--admin", action="store_true")

    token = add("token", cmd_token, "mint an API token for an actor")
    token.add_argument("actor_id")
    token.add_argument("--label")

    ingest = add("ingest", cmd_ingest, "ingest a file or a directory")
    ingest.add_argument("path")
    ingest.add_argument("--actor-id", required=True)
    ingest.add_argument("--storage-root", default="storage")
    ingest.add_argument("--visibility", default="private",
                        choices=("private", "link-view", "link-edit"))
    ingest.add_argument("--extract", action="store_true",
                        help="classify and extract as each document lands")
    ingest.add_argument("--concepts", action="store_true",
                        help="evaluate concepts after extracting")
    ingest.add_argument("--tier", default="local", choices=("local", "cloud"))
    ingest.add_argument("--engine")
    ingest.add_argument("--cloud-opt-in", action="store_true")

    extract = add("extract", cmd_extract, "extract from an ingested document")
    extract.add_argument("document_id")
    extract.add_argument("--actor-id", required=True)
    extract.add_argument("--tier", default="local", choices=("local", "cloud"))
    extract.add_argument("--engine")
    extract.add_argument("--cloud-opt-in", action="store_true")
    extract.add_argument("--force", action="store_true",
                         help="supersede the unreviewed results of an earlier run")

    analyse = add("analyse", cmd_analyse, "compare a document against the corpus")
    analyse.add_argument("document_id")
    analyse.add_argument("--actor-id")

    reviewer = add("review", cmd_review, "confirm, amend or reject one instance")
    reviewer.add_argument("action", choices=("confirm", "amend", "reject"))
    reviewer.add_argument("instance_id")
    reviewer.add_argument("--actor-id", required=True)
    reviewer.add_argument("--set", action="append", default=[],
                          metavar="PROPERTY=VALUE",
                          help="for amend; repeatable")
    reviewer.add_argument("--note")

    report = add("report", cmd_report, "extraction quality, measured")
    report.add_argument("--document-id")
    report.add_argument("--min-reviewed", type=int, default=5)

    config = add("config", cmd_config, "regenerate the Datasette files")
    config.add_argument("--config", default="config/datasette.yml")
    config.add_argument("--storage-root", default="storage")

    searcher = add("search", cmd_search, "search the corpus")
    searcher.add_argument("query", nargs="?")
    searcher.add_argument("--build", action="store_true",
                          help="create the full-text indexes")
    searcher.add_argument("--rebuild", action="store_true",
                          help="with --build, rebuild an existing index")
    searcher.add_argument("--unlinked", action="store_true",
                          help="documents naming this with nothing extracted")
    searcher.add_argument("--limit", type=int, default=50)

    prop = add("property", cmd_property, "rename or drop a property")
    prop.add_argument("action", choices=("rename", "drop"))
    prop.add_argument("type_id")
    prop.add_argument("property_id")
    prop.add_argument("--to", help="new name, for a rename")
    prop.add_argument("--actor-id")
    prop.add_argument("--force", action="store_true",
                      help="for a drop: discard values the column still holds")

    bundle = add("bundle", cmd_bundle, "validate a bundle")
    bundle.add_argument("path", nargs="?")
    bundle.add_argument("--strict", action="store_true",
                        help="fail rather than skip the JSON Schema checks")

    return parser


GLOBAL_DEFAULTS = {"db": DEFAULT_DB, "json": False, "force_lock": False}


def cli(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name, default in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    try:
        return args.func(args) or 0
    except OrpheusError as exc:
        # The core writes its messages for a person, so they are printed as
        # written rather than wrapped in a traceback.
        print(f"orpheus: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(cli())
