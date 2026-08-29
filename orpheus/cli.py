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
from . import quality, review, textract
from .store import Store
from .utils import OrpheusError

DEFAULT_DB = "data/orpheus.sqlite"
#: What `orpheus ingest <directory>` will pick up. Derived from the formats
#: `textract` actually reads rather than listed again here: the two were a copy
#: of each other, and the copy went stale the first time a format was added --
#: `.rst` became ingestable and a directory of it still looked empty.
DOCUMENT_SUFFIXES = tuple(
    sorted("." + suffix for suffix, kind in textract._KINDS.items()
           if kind not in ("unknown", "unsupported_doc")))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _clip(text: object, width: int) -> str:
    """Shorten a name for a column, but never silently.

    A name cut to fit reads as a different name: `Zebra Technologies Inter`
    and `Zebra Technologies International, LLC` are one organisation, and a
    reader cannot tell that from the column. The ellipsis is the difference
    between a shortened name and a wrong one.
    """
    text = "" if text is None else str(text)
    if len(text) <= width:
        return text
    return text[:width - 1] + "\u2026"


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
            line = (f"    {row['table_name']}.{row['property_id']}  "
                    f"{row['n_corrections']}")
            if row.get("example_was") is not None or row.get("example_now") is not None:
                line += f"   e.g. {row['example_was']!r} -> {row['example_now']!r}"
            print(line)

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


def cmd_wiki(args) -> int:
    """The entity wiki: propose, list, read and review pages."""
    from . import entities as entities_mod

    if args.action == "propose":
        store = open_store(args)
        try:
            result = entities_mod.propose_entities(store, type_id=args.type_id,
                                                   actor_id=args.actor_id)
        finally:
            store.close()
        if args.json:
            emit(result, True)
            return 0
        print(f"{result['proposed']} page(s) proposed and "
              f"{result.get('attached', 0)} attached to pages that already "
              f"existed, from {result['linked']} mention(s).")
        for entity in result["entities"]:
            where = "existing" if entity.get("existing") else "new"
            print(f"  {entity['entity_id']}  {_clip(entity['canonical_name'], 38):40} "
                  f"{entity['n_mentions']:>3} mention(s)  via {entity['basis']:<11}"
                  f" {where}")
        if result["entities"]:
            print(f"\n  {result['caveat']}")
        return 0

    if args.action == "list":
        store = open_store(args, mode="read")
        try:
            rows = entities_mod.list_entities(store, type_id=args.type_id,
                                              status=args.status, query=args.query,
                                              limit=args.limit)
        finally:
            store.close()
        if args.json:
            emit({"entities": rows}, True)
            return 0
        for row in rows:
            print(f"  {row['entity_id']}  {row['status']:<12} "
                  f"{_clip(row['canonical_name'], 40):42} "
                  f"{row['n_documents']:>3} doc(s)  "
                  f"{row['n_confirmed'] or 0}/{row['n_mentions']} confirmed")
        return 0

    if args.action == "show":
        if not args.query:
            raise OrpheusError("Give an entity id to show.")
        store = open_store(args, mode="read")
        try:
            page = entities_mod.entity_page(store, args.query,
                                            include_unconfirmed=not args.confirmed_only)
        finally:
            store.close()
        if args.json:
            emit(page, True)
            return 0
        _print_page(page)
        return 0

    raise OrpheusError(f"Unknown action {args.action!r}.")


def cmd_export(args) -> int:
    """Write the wiki out as a portable markdown bundle."""
    from . import export_md

    store = open_store(args, mode="read")
    try:
        result = export_md.export(store, args.out, type_id=args.type_id,
                                  confirmed_only=args.confirmed_only,
                                  limit=args.limit)
    finally:
        store.close()

    if args.json:
        emit(result, True)
        return 0
    print(f"{result['n_files']} file(s) written to {result['root']} "
          f"({result['format']}): {result['n_entities']} page(s), "
          f"{result['n_documents']} source(s).")
    for entry in result["skipped"]:
        # Refused rather than silently dropped: a bundle of uncited assertions
        # is what this format exists to prevent, and so is a quiet omission.
        print(f"  not written: {entry['name']} -- {entry['reason']}")
    return 0


def cmd_graph(args) -> int:
    """The corpus as a network: islands, cut vertices, clusters, neighbourhoods."""
    from . import graph as graph_mod

    store = open_store(args, mode="read")
    try:
        if args.action == "topology":
            result = graph_mod.topology(store, seed=args.seed,
                                        reviewed_only=args.reviewed_only)
        elif args.action == "edges":
            result = {"edges": graph_mod.canonical_edges(
                store, link_type_id=args.link_type,
                reviewed_only=args.reviewed_only),
                "coverage": graph_mod.coverage(store)}
        elif args.action == "path":
            if not args.entity_id or not args.to:
                raise OrpheusError("Give an entity id and --to <entity id>.")
            result = graph_mod.paths_between(store, args.entity_id, args.to,
                                             max_paths=args.max_paths,
                                             max_length=args.max_length)
        elif args.action == "central":
            result = graph_mod.centrality(graph_mod.build(store),
                                          k=args.sample)
        elif args.action == "near":
            if not args.entity_id:
                raise OrpheusError("Give an entity id to look around.")
            result = graph_mod.neighbourhood(store, args.entity_id,
                                             depth=args.depth)
        else:
            raise OrpheusError(f"Unknown action {args.action!r}.")
    finally:
        store.close()

    if args.json:
        emit(result, True)
        return 0

    if args.action == "topology":
        # Coverage first, on purpose: every number after it is conditional on
        # how much of the corpus reached the graph.
        print(result["coverage"]["note"])
        counts = result["counts"]
        print(f"\n  {counts['entities']} page(s), {counts['connected_entities']} "
              f"connected by {counts['canonical_edges']} relation(s)")
        print(f"  {counts['components']} island(s), {counts['isolated_entities']} "
              f"page(s) related to nothing")

        if result["components"]:
            print("\n  islands (deterministic):")
            for island in result["components"]:
                names = ", ".join(_clip(t["name"], 24) for t in island["top_entities"])
                print(f"    {island['n_entities']:>3} page(s)  {names}")
        if result["articulation_points"]:
            print("\n  pages holding the graph together (deterministic):")
            for point in result["articulation_points"]:
                print(f"    {_clip(point['name'], 36):38} {point['degree']} link(s)")
        if result["disconnected_pairs"]:
            print("\n  clusters that never touch (heuristic):")
            for pair in result["disconnected_pairs"][:10]:
                print(f"    {pair['labels'][0][:26]:28} <-> {pair['labels'][1][:26]}")
        if result["isolates"]:
            print(f"\n  related to nothing: "
                  f"{', '.join(_clip(i['name'], 24) for i in result['isolates'][:8])}")
        print(f"\n  {result['note']}")
        return 0

    if args.action == "path":
        print(result["note"] + "\n")
        for path in result["paths"]:
            mark = "checked" if path["confirmed_throughout"] else "UNCHECKED"
            names = " -> ".join(e["name"][:22] for e in path["entities"])
            print(f"  [{mark:>9}] {names}")
            for hop in path["hops"]:
                state = (f"{hop['n_confirmed']} confirmed"
                         if hop["n_confirmed"] else "nobody has checked this")
                print(f"      {_clip(hop['from_name'], 20):22} "
                      f"{(hop['link_type_id'] or '?')[:18]:20} "
                      f"{_clip(hop['to_name'], 20):22} "
                      f"{hop['n_documents']} doc(s), {state}")
            print()
        return 0

    if args.action == "central":
        print(result["note"] + "\n")
        if result["by_betweenness"] is not None:
            print("  by betweenness (sits on paths between others):")
            for node in result["by_betweenness"][:15]:
                print(f"    {_clip(node['name'], 32):34} {node['betweenness']:.4f}  "
                      f"{node['degree']} link(s)")
            print()
        print("  by degree (appears in the most relations):")
        for node in result["by_degree"][:15]:
            print(f"    {_clip(node['name'], 32):34} {node['degree']} link(s)")
        return 0

    if args.action == "edges":
        print(result["coverage"]["note"] + "\n")
        for edge in result["edges"]:
            print(f"  {_clip(edge['from_name'], 24):26} {_clip(edge['link_type_id'], 18):20} "
                  f"{_clip(edge['to_name'], 24):26} {edge['n_documents']} doc(s)")
        return 0

    entity = result["entity"]
    print(f"{entity['canonical_name']}  [{entity['type_id']}]  "
          f"{result['n_nodes']} page(s), {result['n_edges']} relation(s) within "
          f"{result['depth']} hop(s)\n")
    for edge in result["edges"]:
        print(f"  {_clip(edge['from_name'], 24):26} {_clip(edge['link_type_id'], 18):20} "
              f"{_clip(edge['to_name'], 24):26} {edge['n_documents']} doc(s)")
    return 0


def cmd_corroboration(args) -> int:
    """Where the corpus agrees with itself -- and where it is quoting itself."""
    from . import corroboration as corroboration_mod

    store = open_store(args, mode="read")
    try:
        if args.entity_id:
            result = corroboration_mod.for_entity(store, args.entity_id)
        else:
            result = corroboration_mod.summary(
                store, min_documents=args.min_documents)
    finally:
        store.close()

    if args.json:
        emit(result, True)
        return 0

    if "headline" in result:
        print(result["headline"] + "\n")
    for claim in result["properties"]:
        mark = "*" if claim["independent"] else "copied"
        print(f"  [{mark:>6}] {_clip(claim['subject_name'], 28):30} "
              f"{claim['property_id']:<16} {str(claim['value'])[:26]:28} "
              f"{claim['n_documents']} doc(s) / {claim['n_wordings']} wording(s)")
    for claim in result["relations"]:
        mark = "*" if claim["independent"] else "copied"
        print(f"  [{mark:>6}] {_clip(claim['from_name'], 24):26} "
              f"{_clip(claim['link_type_id'], 16):18} {_clip(claim['to_name'], 24):26} "
              f"{claim['n_documents']} doc(s) / {claim['n_wordings']} wording(s)")
    if "note" in result:
        print(f"\n  {result['note']}")
    return 0


def cmd_budget(args) -> int:
    """What has been sent to the cloud tier, against the cap."""
    from .llm import budget_status

    if args.set_limit is not None or args.window or args.price is not None:
        store = open_store(args)
        try:
            if args.set_limit is not None:
                store.set_setting("cloud_budget_chars", str(args.set_limit),
                                  args.actor_id)
            if args.window:
                store.set_setting("cloud_budget_window", args.window,
                                  args.actor_id)
            if args.price is not None:
                store.set_setting("cloud_price_per_million_chars",
                                  str(args.price), args.actor_id)
            status = budget_status(store)
        finally:
            store.close()
    else:
        store = open_store(args, mode="read")
        try:
            status = budget_status(store)
        finally:
            store.close()

    if args.json:
        emit(status, True)
        return 0
    print(status["note"])
    print(f"  {status['estimated_cost_note']}")
    if status["estimated_cost"] is not None:
        print(f"  estimated: {status['estimated_cost']}")
    # Non-zero when spent, so a corpus script can stop before the gate refuses.
    return 1 if status["exceeded"] else 0


def cmd_questions(args) -> int:
    """What the shape of the corpus raises. None of it is a finding."""
    from . import questions as questions_mod

    if args.review:
        if not args.status or not args.note:
            raise OrpheusError(
                "Recording a judgement needs --status and --note. The reason is "
                "the part worth anything to the next reviewer.")
        store = open_store(args)
        try:
            result = questions_mod.review_question(
                store, args.review, args.status, args.note,
                actor_id=args.actor_id)
        finally:
            store.close()
        if args.json:
            emit(result, True)
            return 0
        print(f"Recorded: {result['status']}. {result['rationale']}")
        return 0

    store = open_store(args, mode="read")
    try:
        report = questions_mod.raised(store, open_only=args.open_only)
    finally:
        store.close()

    if args.json:
        emit(report, True)
        return 0

    # Coverage first: two of the three checks read the relation graph, and a
    # question never raised because the evidence never got there is the one
    # nobody will think to look for.
    print(report["coverage"]["note"])
    print(f"\n{report['note']}\n")
    for question in report["questions"]:
        if args.confirmed_only and not question["confirmed_throughout"]:
            continue
        mark = ("standing" if question["status"] == "standing"
                else question["status"] if question["status"] != "open"
                else "checked" if question["confirmed_throughout"]
                else "unreviewed")
        print(f"  [{mark:>10}] {question['summary']}")
        print(f"        {question['fingerprint']}"
              + ("  (judgement made against different evidence)"
                 if question["review_stale"] else ""))
        if question.get("review") and not question["review_stale"]:
            print(f"        {question['review']['rationale']}")
        for hop in question["chain"]:
            if "from_name" in hop:
                state = (f"{hop['n_confirmed']} confirmed"
                         if hop["n_confirmed"] else "nobody has checked this")
                print(f"        {_clip(hop['from_name'], 22):24} "
                      f"{_clip(hop['link_type_id'], 16):18} {_clip(hop['to_name'], 22):24} "
                      f"{hop['n_documents']} doc(s), {state}")
            else:
                print(f"        {hop.get('part', '?'):<16} "
                      f"{hop.get('filename', hop.get('document_id', ''))[:28]:30} "
                      f"{hop.get('status', '')}")
        print(f"        -> {question['asks']}\n")
    return 0


def cmd_read(args) -> int:
    """Read a document a passage at a time, with the machine offering."""
    from . import companion

    if args.accept or args.dismiss:
        store = open_store(args)
        try:
            if args.accept:
                properties = dict(pair.split("=", 1) for pair in args.set or [])
                result = companion.accept_suggestion(
                    store, args.accept, args.actor_id,
                    properties=properties or None, note=args.note)
            else:
                result = companion.dismiss_suggestion(
                    store, args.dismiss, args.actor_id, note=args.note)
        finally:
            store.close()
        if args.json:
            emit(result, True)
            return 0
        print(f"{result['suggestion_id']} is now {result['status']}."
              + (f" Recorded as {result['instance_id']}."
                 if result.get("instance_id") else ""))
        return 0

    if args.page is None:
        store = open_store(args, mode="read")
        try:
            progress = companion.reading_progress(store, args.document_id,
                                                  actor_id=args.actor_id)
        finally:
            store.close()
        if args.json:
            emit(progress, True)
            return 0
        print(progress["note"])
        if progress["unread"]:
            print(f"  unread: {', '.join(str(n) for n in progress['unread'])}")
        return 0

    store = open_store(args)
    try:
        result = companion.read_passage(
            store, args.document_id, args.page, actor_id=args.actor_id,
            engine=args.engine, tier=args.tier, opt_in=args.cloud_opt_in,
            context_chars=args.context_chars)
    finally:
        store.close()

    if args.json:
        emit(result, True)
        return 0
    if not result["suggestions"]:
        print(f"Page {args.page} read, and nothing stood out. That is recorded "
              "too -- a page nobody opened and a page holding nothing are "
              "different things.")
        return 0
    print(f"Page {args.page}: {result['n_offered']} thing(s) worth a look. "
          "None of this is in the store until you record it.\n")
    for offer in result["suggestions"]:
        values = ", ".join(f"{k}={v}" for k, v in offer["properties"].items()
                           if k != "page_no")
        print(f"  {offer['suggestion_id']}  {offer['type_id']:<16} {values}")
        print(f"      {' '.join((offer['excerpt'] or '').split())[:76]!r}")
    return 0


def cmd_migrate(args) -> int:
    """Bring a store's schema up to what this build expects.

    Its own command because Datasette holds a shared connection and cannot
    migrate under itself: an upgrade is stop the server, migrate, start it.
    """
    store = open_store(args, mode="read")
    try:
        pending = store.pending_migrations()
    finally:
        store.close()

    if not pending:
        if args.json:
            emit({"applied": [], "pending": []}, True)
            return 0
        print("Already current. Nothing to apply.")
        return 0
    if args.check:
        if args.json:
            emit({"applied": [], "pending": pending}, True)
            return 1
        print(f"Behind: migration(s) {pending} have not been applied.")
        # Non-zero so a deployment script can gate a restart on it.
        return 1

    # Opening for write migrates on the way in, so `store.migrate()` here would
    # report nothing applied and read as a no-op. What was pending a moment ago
    # is the honest answer.
    store = open_store(args)
    try:
        store.migrate()
        remaining = store.pending_migrations()
    finally:
        store.close()
    if args.json:
        emit({"applied": pending, "pending": remaining}, True)
        return 0
    print(f"Applied migration(s) {pending}.")
    return 0


def cmd_register(args) -> int:
    """Bring a register in, look it over, and promote it when it is right.

    A register is reference data and never becomes a fact: its rows are held
    apart from the corpus and feed the evidence a person weighs when deciding
    whether two pages are one thing. Nothing counts until `--promote`.
    """
    from . import registers as registers_mod

    if args.list:
        store = open_store(args)
        try:
            found = registers_mod.list_registers(store)
        finally:
            store.close()
        if args.json:
            emit(found, True)
            return 0
        if not found:
            print("No registers. `orpheus register --add file.csv --name ...`.")
            return 0
        for row in found:
            rejected = f", {row['n_rejected']} rejected" if row["n_rejected"] else ""
            print(f"  {row['register_id']}  {_clip(row['name'], 28):28} "
                  f"{row['status']:10} {row['n_rows']} row(s){rejected}")
        return 0

    store = open_store(args, mode="write")
    try:
        if args.add:
            register_id = registers_mod.create_register(
                store, args.name or Path(args.add).stem,
                description=args.description, origin=args.origin or args.add,
                actor_id=args.actor_id)
            result = registers_mod.load_csv(
                store, register_id, Path(args.add).read_text(),
                name_column=args.name_column,
                identifier_column=args.identifier_column,
                type_id=args.type_id, actor_id=args.actor_id)
            store.conn.commit()
        elif args.reject is not None:
            result = registers_mod.review_row(
                store, args.register_id, args.reject, "rejected",
                note=args.note, actor_id=args.actor_id)
            store.conn.commit()
        elif args.promote:
            result = registers_mod.promote(store, args.register_id,
                                           actor_id=args.actor_id,
                                           note=args.note)
            store.conn.commit()
        elif args.withdraw:
            result = registers_mod.withdraw(store, args.register_id,
                                            actor_id=args.actor_id,
                                            note=args.note)
            store.conn.commit()
        else:
            result = {"register": registers_mod.get_register(
                          store, args.register_id),
                      "rows": registers_mod.rows(store, args.register_id,
                                                 limit=args.limit)}
    finally:
        store.close()

    if args.json:
        emit(result, True)
        return 0

    if args.add:
        print(f"{result['n_rows']} row(s) staged as {result['register_id']}.")
        print(f"  {result['caveat']}")
        print("  Staged means readable and not yet evidence. Look it over, "
              "then --promote.")
    elif "rows" in result:
        register = result["register"]
        print(f"{register['name']} -- {register['status']}")
        if register["status"] != "active":
            print("  Not evidence yet. --promote when it is right.")
        for row in result["rows"]:
            mark = " " if row["status"] == "accepted" else \
                   "x" if row["status"] == "rejected" else "?"
            print(f"  {mark} {row['row_no']:4} {_clip(row['name'] or '', 34):34} "
                  f"{row['identifier'] or ''}")
    elif args.promote:
        print(f"{result['rows_accepted']} row(s) accepted. It counts as "
              "evidence now, and a judgement made before it is stale.")
    elif args.withdraw:
        print("Withdrawn. It stops counting and stays readable, because a "
              "register somebody relied on is part of how a decision was made.")
    else:
        print(f"Row {result['row_no']} is {result['status']}.")
    return 0


def cmd_record(args) -> int:
    """Record a fact a person read in a document, with the line they read it on."""
    from . import record as record_mod

    if not args.set:
        raise OrpheusError("Nothing to record. Use --set name=... at least once.")
    properties = dict(pair.split("=", 1) for pair in args.set)

    store = open_store(args, mode="write")
    try:
        result = record_mod.record_fact(
            store, args.document_id, args.type_id, properties,
            quote=args.quote, actor_id=args.actor_id, note=args.note)
    finally:
        store.close()

    if args.json:
        emit(result, True)
        return 0
    print(f"  {result['instance_id']}  {result['type_id']}  "
          f"page {result['page_no']}  [{result['alignment']}]")
    print(f"  quoted: {result['excerpt'][:110]!r}")
    print(f"\n  {result['note']}")
    return 0


def cmd_lint(args) -> int:
    """Look for where the store is misleading a reader, and say where."""
    from . import lint as lint_mod

    store = open_store(args, mode="read")
    try:
        report = lint_mod.lint(store, deep=not args.shallow,
                               document_id=args.document_id,
                               checks=args.check or None)
    finally:
        store.close()

    if args.json:
        emit(report, True)
        return 0

    print(report["headline"])
    print()
    for found in report["findings"]:
        where = found["where"]
        # The located part. A finding a person cannot open is one nobody acts on.
        anchor = (where.get("entity_id") or where.get("instance_id")
                  or where.get("document_id") or where.get("tension_id") or "")
        print(f"  [{found['severity']:>6}] {found['check']}")
        print(f"           {found['finding']}")
        print(f"           {anchor}"
              + (f"  {where['name']}" if where.get("name") else "")
              + (f"  {where['filename']}" if where.get("filename") else ""))
        print(f"           -> {found['suggestion']}")
        print()
    # Non-zero on a high finding, so this can gate a corpus run in a script.
    return 1 if report["counts"]["high"] else 0


def cmd_tension(args) -> int:
    """Conflicts that survive review: find them, sign them, settle them."""
    from . import tensions as tensions_mod

    if args.action == "find":
        store = open_store(args, mode="read")
        try:
            conflicts = tensions_mod.detect_conflicts(
                store, entity_id=args.subject, type_id=args.type_id,
                reviewed_only=not args.include_unreviewed)
        finally:
            store.close()
        if args.json:
            emit({"conflicts": conflicts}, True)
            return 0
        if not conflicts:
            print("  No property has two different reviewed values. That is a "
                  "real answer only if enough has been reviewed to argue about.")
            return 0
        for conflict in conflicts:
            mark = "recorded" if conflict["existing_tension_id"] else "NEW"
            print(f"  [{mark:>8}] {_clip(conflict['subject_name'], 32):34} "
                  f"{conflict['property_id']:<18} "
                  f"{conflict['n_values']} values")
        return 0

    if args.action == "propose":
        store = open_store(args)
        try:
            result = tensions_mod.propose_tensions(
                store, actor_id=args.actor_id, entity_id=args.subject,
                type_id=args.type_id, reviewed_only=not args.include_unreviewed)
        finally:
            store.close()
        if args.json:
            emit(result, True)
            return 0
        print(f"{result['n_raised']} raised, {result['already_recorded']} already "
              f"recorded, of {result['n_conflicts']} conflict(s) found.")
        return 0

    if args.action == "list":
        store = open_store(args, mode="read")
        try:
            rows = tensions_mod.list_tensions(
                store, subject_id=args.subject, status=args.status,
                kind=args.kind, open_only=args.standing, limit=args.limit)
        finally:
            store.close()
        if args.json:
            emit({"tensions": rows}, True)
            return 0
        for row in rows:
            print(f"  {row['tension_id']}  {row['status']:<10} "
                  f"{row['kind']:<20} {row['summary'][:60]}")
            for side in row["sides"]:
                excerpt = " ".join((side.get("excerpt") or "").split())[:40]
                print(f"      {side.get('filename', '?')[:20]:22} "
                      f"p{side.get('page_no') or '?':<3} "
                      f"{_clip(side.get('position') or '', 24):26} {excerpt!r}")
        return 0

    verbs = {"accept": tensions_mod.accept_tension,
             "resolve": tensions_mod.resolve_tension,
             "withdraw": tensions_mod.withdraw_tension}
    if args.action in verbs:
        if not args.subject:
            raise OrpheusError(f"Give a tension id to {args.action}.")
        if args.action != "accept" and not args.note:
            raise OrpheusError(
                f"--note is required to {args.action}: a conflict settled with "
                "no account of the reasoning looks decided and cannot be checked.")
        store = open_store(args)
        try:
            if args.action == "accept":
                result = verbs["accept"](store, args.subject, args.actor_id,
                                         note=args.note)
            else:
                result = verbs[args.action](store, args.subject, args.actor_id,
                                            args.note)
        finally:
            store.close()
        if args.json:
            emit(result, True)
            return 0
        print(f"{result['tension_id']} is now {result['status']}.")
        return 0

    raise OrpheusError(f"Unknown action {args.action!r}.")


def _print_page(page: dict) -> None:
    entity = page["entity"]
    print(f"{entity['canonical_name']}  [{entity['type_id']}] "
          f"({entity['status']})")
    if page["description"]:
        print(f"\n  {page['description']}")
    if page["aliases"]:
        print(f"\n  also written: {', '.join(page['aliases'])}")

    counts = page["counts"]
    print(f"\n  {counts['mentions']} mention(s) across {counts['documents']} "
          f"document(s); {counts['confirmed_links']} confirmed, "
          f"{counts['unconfirmed_links']} awaiting review")

    if page["properties"]:
        print("\n  what the documents say:")
        for prop, values in page["properties"].items():
            for value in values:
                mark = "*" if value["n_confirmed"] else " "
                print(f"   {mark} {prop:<22} {str(value['value'])[:30]:32} "
                      f"{len(value['mentions'])} mention(s)")

    print("\n  sources:")
    for record in page["mentions"]:
        evidence = record["evidence"] or {}
        document = record["document"] or {}
        excerpt = " ".join((evidence.get("excerpt") or "").split())[:44]
        print(f"    {_clip(document.get('filename', '?'), 22):24} "
              f"p{evidence.get('page_no') or '?':<3} "
              f"{record['link']['status']:<12} {excerpt!r}")

    if page["caveat"]:
        print(f"\n  {page['caveat']}")


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
            result = search_mod.unextracted_mentions(store, args.query,
                                                    limit=args.limit)
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


def cmd_ontology(args) -> int:
    """Survey a corpus that has no ontology, review what it proposes, draft one.

    Four verbs, one loop: `survey` proposes, `candidates` lists what it
    proposed, `review` decides, `draft` assembles a bundle out of the decisions.
    Nothing between the first and the last writes a bundle -- see
    `orpheus/ontology.py` on why that line is where it is.
    """
    from . import ontology

    store = open_store(args, mode="read" if args.action == "candidates"
                       else "write")
    try:
        if args.action == "survey":
            result = ontology.survey(
                store, engine=args.engine or ontology.DEFAULT_ENGINE,
                sample=args.sample, actor_id=args.actor_id, tier=args.tier,
                opt_in=args.cloud_opt_in, min_support=args.min_support,
                document_ids=args.document_id or None,
                primary_type=args.type_id or ontology.DEFAULT_PRIMARY_TYPE,
                chars_per_document=args.chars_per_document)
        elif args.action == "candidates":
            result = {"candidates": ontology.candidates(
                store, status=args.status, kind=args.kind)}
        elif args.action == "reopen":
            if not args.candidate_id:
                raise OrpheusError("Give a candidate id to reopen.")
            result = ontology.reopen_candidate(store, args.candidate_id,
                                               args.actor_id, note=args.note)
        elif args.action == "review":
            if not args.candidate_id:
                raise OrpheusError("Give a candidate id to review.")
            if not args.decision:
                raise OrpheusError(
                    "Give --decision accepted or --decision rejected.")
            result = ontology.review_candidate(
                store, args.candidate_id, args.decision, args.actor_id,
                accepted_as=args.to, note=args.note)
        else:
            result = ontology.draft_bundle(
                store, args.bundle_id, bundle_version=args.bundle_version,
                name=args.name, primary_type=args.type_id or None,
                document_types=args.document_type or None,
                document_scoped=args.document_scoped or None)
            if args.out:
                Path(args.out).write_text(
                    json.dumps(result["bundle"], indent=2) + "\n")
                result["written"] = args.out
            # Registering is a separate act, on purpose. A drafting command
            # that also installed the ontology would be the one place an
            # ontology arrived in a store without anybody choosing it.
            if args.register:
                if result["problems"]:
                    raise OrpheusError(
                        "This draft has problems, so it is not being "
                        "registered:\n  - " + "\n  - ".join(result["problems"]))
                bundle_mod.register(store, result["bundle"],
                                    actor_id=args.actor_id, activate=True)
                bundle_mod.apply_schema(store, result["bundle"])
                result["registered"] = True
            result.pop("bundle", None)
    finally:
        store.close()
    emit(result, args.json)
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

    onto = add("ontology", cmd_ontology,
               "propose an ontology for a corpus that has none")
    onto.add_argument("action",
                      choices=("survey", "candidates", "review", "reopen",
                               "draft"))
    onto.add_argument("candidate_id", nargs="?",
                      help="for `review` and `reopen`")
    onto.add_argument("--actor-id")
    onto.add_argument("--engine",
                      help="deterministic (default), or a general model")
    onto.add_argument("--sample", type=int, default=20,
                      help="how many documents to read")
    onto.add_argument("--chars-per-document", type=int, default=6000,
                      help="how much of each document a model is shown")
    onto.add_argument("--min-support", type=int, default=2,
                      help="documents a shape must appear in to be proposed")
    onto.add_argument("--document-id", action="append", default=[],
                      help="survey these documents; repeatable")
    onto.add_argument("--tier", default="local", choices=("local", "cloud"))
    onto.add_argument("--cloud-opt-in", action="store_true")
    onto.add_argument("--status", default="proposed")
    onto.add_argument("--kind", choices=("object_type", "property",
                                         "link_type"))
    onto.add_argument("--decision", choices=("accepted", "rejected"))
    onto.add_argument("--to", help="accept it under this name instead")
    onto.add_argument("--note")
    onto.add_argument("--bundle-id", default="drafted-core")
    onto.add_argument("--bundle-version", default="0.1.0")
    onto.add_argument("--name", help="the bundle's display name")
    onto.add_argument("--type-id",
                      help="the type to hang fields on, or the primary type")
    onto.add_argument("--document-type", action="append", default=[],
                      help="the classifier's vocabulary; repeatable")
    onto.add_argument("--document-scoped", action="append", default=[],
                      help="types whose identity is the document; repeatable")
    onto.add_argument("--out", help="write the drafted bundle here")
    onto.add_argument("--register", action="store_true",
                      help="register and apply the draft, if it has no problems")

    config = add("config", cmd_config, "regenerate the Datasette files")
    config.add_argument("--config", default="config/datasette.yml")
    config.add_argument("--storage-root", default="storage")

    wiki = add("wiki", cmd_wiki, "the entity wiki")
    wiki.add_argument("action", choices=("propose", "list", "show"))
    wiki.add_argument("query", nargs="?", help="entity id for `show`, filter for `list`")
    wiki.add_argument("--type-id")
    wiki.add_argument("--status")
    wiki.add_argument("--actor-id")
    wiki.add_argument("--confirmed-only", action="store_true",
                      help="show only what a person has confirmed")
    wiki.add_argument("--limit", type=int, default=100)

    exporter = add("export", cmd_export, "write the wiki out as markdown")
    exporter.add_argument("out", help="directory to write the bundle into")
    exporter.add_argument("--type-id")
    exporter.add_argument("--confirmed-only", action="store_true",
                          help="leave out anything a person has not checked")
    exporter.add_argument("--limit", type=int, default=1000)

    grapher = add("graph", cmd_graph, "the corpus as a network")
    grapher.add_argument("action", choices=("topology", "edges", "near",
                                            "path", "central"))
    grapher.add_argument("entity_id", nargs="?",
                         help="entity id for `near` and `path`")
    grapher.add_argument("--to", help="the other end, for `path`")
    grapher.add_argument("--max-paths", type=int, default=5)
    grapher.add_argument("--sample", type=int,
                         help="approximate betweenness from this many sources")
    grapher.add_argument("--depth", type=int, default=1,
                         help="hops out from the page, for `near`")
    grapher.add_argument("--max-length", type=int, default=6,
                         help="longest chain to report, for `path`")
    grapher.add_argument("--link-type")
    grapher.add_argument("--seed", type=int, default=20260824,
                         help="community detection is seeded; change to see "
                              "how stable the partition is")
    grapher.add_argument("--reviewed-only", action="store_true")

    agreeing = add("corroboration", cmd_corroboration,
                   "where the corpus agrees with itself")
    agreeing.add_argument("entity_id", nargs="?")
    agreeing.add_argument("--min-documents", type=int, default=2)

    budget = add("budget", cmd_budget, "the cloud tier's cap, and what is left")
    budget.add_argument("--set-limit", type=int,
                        help="characters that may be sent per window")
    budget.add_argument("--window", choices=("total", "day", "month"))
    budget.add_argument("--price", type=float,
                        help="this deployment's rate per million characters, "
                             "for an estimate that is labelled an estimate")
    budget.add_argument("--actor-id")

    asker = add("questions", cmd_questions,
                "what the shape of the corpus raises -- none of it a finding")
    asker.add_argument("--confirmed-only", action="store_true",
                       help="only chains where every hop has been confirmed")
    asker.add_argument("--open-only", action="store_true",
                       help="hide questions somebody has already settled")
    asker.add_argument("--review", metavar="FINGERPRINT",
                       help="record a judgement about one question")
    asker.add_argument("--status",
                       choices=("standing", "explained", "dismissed"),
                       help="standing: real, and it stays on the list")
    asker.add_argument("--note", help="why. Required with --review.")
    asker.add_argument("--actor-id")

    reader = add("read", cmd_read, "read a document a passage at a time")
    reader.add_argument("document_id")
    reader.add_argument("--page", type=int,
                        help="the passage to read; omit for progress")
    reader.add_argument("--actor-id")
    reader.add_argument("--engine", default="deterministic")
    reader.add_argument("--tier", default="local", choices=("local", "cloud"))
    reader.add_argument("--cloud-opt-in", action="store_true")
    reader.add_argument(
        "--context-chars", type=int, default=0, metavar="N",
        help="let the model see up to N characters of the rest of the document "
             "behind the page. Off by default: the context is charged to the "
             "same budget as the passage.")
    reader.add_argument("--accept", metavar="SUGGESTION_ID")
    reader.add_argument("--dismiss", metavar="SUGGESTION_ID")
    reader.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="correct a field while accepting (repeatable)")
    reader.add_argument("--note")

    migrator = add("migrate", cmd_migrate, "bring the schema up to this build")
    migrator.add_argument("--check", action="store_true",
                          help="report what is pending and exit non-zero, "
                               "without applying anything")

    register = add("register", cmd_register,
                   "bring in reference data a person vouches for")
    register.add_argument("register_id", nargs="?",
                          help="an existing register, to show or act on")
    register.add_argument("--list", action="store_true",
                          help="every register and its state")
    register.add_argument("--add", metavar="FILE.csv",
                          help="stage a delimited file as a new register")
    register.add_argument("--name", help="what to call it")
    register.add_argument("--description")
    register.add_argument("--origin", help="where it came from, in your words")
    register.add_argument("--type-id",
                          help="the bundle type its names are, e.g. Company, "
                               "so they normalise the same way the wiki's do")
    register.add_argument("--name-column",
                          help="which column holds the name, if the guess "
                               "would be wrong")
    register.add_argument("--identifier-column",
                          help="which column holds the registered number")
    register.add_argument("--reject", type=int, metavar="ROW_NO",
                          help="mark one row as not to be used")
    register.add_argument("--promote", action="store_true",
                          help="vouch for it, and let it count as evidence")
    register.add_argument("--withdraw", action="store_true",
                          help="stop it counting, without deleting it")
    register.add_argument("--limit", type=int, default=20)
    register.add_argument("--note")
    register.add_argument("--actor-id")

    recorder = add("record", cmd_record,
                   "record a fact a person read that extraction missed")
    recorder.add_argument("document_id")
    recorder.add_argument("--type-id", required=True,
                          help="the bundle type, e.g. Person or Company")
    recorder.add_argument("--set", action="append", metavar="PROPERTY=VALUE",
                          help="a value to record; repeatable")
    recorder.add_argument("--quote", required=True,
                          help="the text in the document you read it on; it is "
                               "located there, and refused if it is not")
    recorder.add_argument("--actor-id", required=True)
    recorder.add_argument("--note")

    linter = add("lint", cmd_lint, "look for where the store misleads a reader")
    linter.add_argument("--document-id")
    linter.add_argument("--shallow", action="store_true",
                        help="skip the checks that compare every mention")
    linter.add_argument("--check", action="append",
                        help="run only this check (repeatable)")

    tension = add("tension", cmd_tension, "conflicts that survive review")
    tension.add_argument("action", choices=("find", "propose", "list", "accept",
                                            "resolve", "withdraw"))
    tension.add_argument("subject", nargs="?",
                         help="entity id for find/propose, tension id to settle")
    tension.add_argument("--type-id")
    tension.add_argument("--status")
    tension.add_argument("--kind")
    tension.add_argument("--actor-id")
    tension.add_argument("--note", help="why. Required to resolve or withdraw.")
    tension.add_argument("--standing", action="store_true",
                         help="only conflicts still live on a page")
    tension.add_argument("--include-unreviewed", action="store_true",
                         help="argue over unconfirmed extractions too")
    tension.add_argument("--limit", type=int, default=200)

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
