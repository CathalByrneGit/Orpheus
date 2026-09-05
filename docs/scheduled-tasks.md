← [Back to index](index.md)

# Work on a clock

Some of what Orpheus does wants a schedule rather than a button. A storage
verify is slow on purpose and its whole job is catching a bad restore before
somebody finds out one document at a time. A wiki queue should be populated
when a reviewer arrives, not after somebody remembers to populate it. A calendar
that only speaks when something is overdue is useless if nobody asks it.

None of that is new. What was missing is a place to run it.

## Why a crontab could not do this

Two reasons, and the plain one first.

**The shipped deployment has no cron daemon.** `deploy/Dockerfile` builds one
process on `python:3.11-slim` — `datasette serve` — with no cron installed and
nothing to install it into. A scheduled task there has nowhere to run at all.

**And where a crontab does exist, running one is worse than it looks.**
`orpheus wiki propose` opens the store as a second *writing process*, and
nothing refuses it — that was checked rather than assumed. The advisory lock is
taken by `Store(mode="write")`, and Datasette never takes it, because it borrows
a connection it already opened. So the guard people assume is standing there is
not:

```
$ orpheus --db /data/orpheus.sqlite wiki propose      # server running
Nothing to propose: all 3 extracted instance(s) are already on a page.
```

That is not permission — it is an unsupervised second writer. A `mode="write"`
open **applies migrations**, so a command run from cron after an upgrade can
move the schema under a server that has already loaded it; `assert_current()`
exists in the plugin precisely because that mismatch happens. And its writes
contend with the server's at the SQLite level, with a five-second `busy_timeout`
between the corpus and `database is locked`.

What the lock *does* refuse is a second CLI writer, which is the shape a
crontab overlapping a maintenance script actually takes:

```
$ orpheus --db /data/orpheus.sqlite scheduled wiki-propose
orpheus: The single-writer lock on /data/orpheus.sqlite is held by pid 2497.
Only one process may write to the store — send writes through the running
Datasette instead of opening a second writer.
```

The error names the remedy, and this is it. Anything scheduled belongs
**inside the process that owns the database**.
[`datasette-cron`](https://github.com/datasette/datasette-cron) runs in-process
and hands each handler the `Datasette` instance, so a task goes through
`execute_write_fn` — the same write thread every other Orpheus write already
goes through. It fits the single-writer design rather than working around it.

It stores its own task and run tables in Datasette's **internal** database, so
nothing here pollutes the Orpheus schema or shows up as corpus data.

## The four tasks

`orpheus scheduled` lists them, and says which this install can actually run.

| Task | Writes | Suggested | What it does |
|---|---|---|---|
| `verify` | no | `0 3 * * *` | Re-reads every original and checks it against the digest recorded at ingest |
| `search-index` | yes | `10 3 * * *` | Makes sure the full-text index exists |
| `wiki-propose` | yes | `0 4 * * *` | Groups unlinked mentions and proposes an entity page each |
| `calendar-digest` | no | `0 8 * * 1` | What falls due, weekly |

**`verify` is the one that most wants a schedule.** A database and a `storage/`
restored from two different moments look perfectly healthy from the inside:
every row is there, every excerpt renders, and the character offsets point into
bytes nobody has compared to anything. Nothing else in Orpheus would notice.
The check costs the size of the corpus in disk reads, which is exactly why it
belongs at three in the morning rather than behind a button somebody stops
pressing.

**`search-index` is a reconcile, not a rebuild** — and the honest version of a
claim made too quickly when this was first proposed. The FTS triggers keep the
index in step with `document_pages` and `provenance` by themselves; that was
measured, not assumed, and an index does *not* go stale as documents arrive.
What it does close is a real gap: a deployment where nobody ever ran
`orpheus search --build` has no index at all, and the chat's first answer is a
polite explanation that it cannot search. It is a no-op once built. Pass
`config: {rebuild: true}` to make it a genuine rebuild.

**`wiki-propose` is the one that could not run at all before**, because it
writes. Everything it makes is `unconfirmed` and linked on `naive_key`, the
weakest basis there is: it turns mentions into a queue and decides nothing.

## Three things a schedule changes, and what was done about each

### Nobody is present to be named

Every write carries an actor, and a clock has none. Borrowing the administrator
who set the schedule would put their name on months of rows they never saw.

So there is a machine actor, filed under `idp = 'orpheus-scheduled'`, and it is
**never an administrator**. `auth.create_token` refuses to mint it a
credential — a machine actor is a label on work the deployment did for itself,
and the moment it can hold a token it stops being a label and becomes a way in
that nobody is watching.

The audit trail can therefore tell scheduled work from human work by reading
the row, which matters because `orpheus report` counts review: if a robot's
decisions were indistinguishable from a person's, the number that says how much
of the corpus a human has checked would be quietly wrong.

### Nobody is present to opt in

The cloud gate asks two independent questions: has the organisation enabled
cloud processing, and did this person opt in for this request. The second is
deliberately taken *each time* rather than once at setup, because sending a
document to a third party is a decision.

A schedule cannot take that decision. So every scheduled run is wrapped in
`llm.no_cloud()`, which refuses any cloud call inside it whatever the policy
says, before either of the gate's own conditions is consulted:

```
A scheduled run may not send anything to a cloud model. The opt-in the gate
asks for is a person deciding to send this document to a third party, taken
each time -- and a clock cannot take it. Run the pass from the surface or the
command line, where somebody is present to opt in.
```

That is enforcement rather than a convention. None of the four tasks calls a
model today; the point is that one added later cannot start to without being
refused.

The refusal is installed inside `run_chunk`, which is the function that actually
runs on the store's thread. A `ContextVar` set on the event loop does not cross
`run_in_executor`, so wrapping the coroutine that *queues* the work would look
right and hold nothing.

### Nobody is watching the connection

Datasette answers pages from a small pool of read connections. A verify over a
large corpus is minutes of hashing, and one call holding one of those
connections for minutes is not a background job — it is an outage at three in
the morning.

So a task is expressed in **chunks**: `chunks()` is one cheap read saying what
the work is, `run_chunk()` does one unit of it, and `combine()` adds the units
up without touching the store at all. The plugin awaits one chunk at a time and
lets go of the connection between them, so a nightly verify is a sequence of
short holds rather than one long one. The CLI runs the same chunks in a loop.

Because `combine()` is pure and shared, a batched run and a single pass are
guaranteed to agree about what the corpus looks like — that is a test, not a
hope.

## Setting it up

```
pip install 'orpheus[cron]'
```

Handlers register themselves whenever `datasette-cron` is installed, so tasks
can be added from `/-/cron` with nothing else configured. To declare them in
config instead:

```yaml
plugins:
  orpheus-cron:
    tasks:
      verify: "0 3 * * *"
      search-index:
        schedule: default
      wiki-propose:
        schedule: {interval: 3600}
      calendar-digest:
        schedule: "0 8 * * 1"
        config: {within_days: 30}
```

A bare string is a cron expression; `{interval: seconds}` and `{rrule: ...}`
are the other two forms `datasette-cron` takes; `default` uses the task's own
recommended cadence. Tasks are created as `orpheus-<name>`.

**Config is the source of truth for the tasks it names.** A schedule edited in
the cron UI is reset on the next restart, because `add_task` is an upsert. A
deployment that would rather manage schedules in the UI should leave this empty
and add them there. `enabled` is never written by the upsert, so a task *paused*
in the UI stays paused across restarts.

A bad entry is one task that does not exist, not a startup that fails — the
others still register, and the log says which one and why:

```
orpheus cron: nonsense not scheduled: No scheduled task named 'nonsense'.
Known: calendar-digest, search-index, verify, wiki-propose.
```

### The scheduler starts on the first HTTP request

This is `datasette-cron`'s design, not something Orpheus can change: the
scheduler loop starts when the first non-lifespan request arrives, after every
startup hook has run. **A Datasette that receives no traffic runs no tasks.**

An internal tool that sits idle overnight would simply not run the nightly
verify — with no error, because nothing tried.

The container already solves this and did before this feature existed:
`deploy/Dockerfile` carries a `HEALTHCHECK` hitting `/-/orpheus/api/health`
every 30 seconds, which is traffic, so a Docker deployment keeps its scheduler
alive with nothing added. A deployment running Datasette some other way needs
to arrange the same — that endpoint needs no authentication and touches no
document. [Deployment](deployment.md) says so beside the WAL trap, because both
are things that get discovered rather than read.

## What a run says

Silence is the design. A task that finds nothing worth reporting logs its
headline at `INFO`, which Datasette does not print by default.

A task that finds something **raises**, and `datasette-cron` records the run as
failed with the message. That is deliberately the same contract `orpheus verify`
and `orpheus calendar` already keep at a terminal: both exit non-zero when they
have something to say, precisely so a schedule can be quiet when they do not. A
red row in `/-/cron` carrying the headline is that same signal in a place
somebody will see it:

```
error   | 1 of 2 originals cannot be served: 1 altered. Every excerpt taken
          from them is now unverifiable.
success | None
```

It is not a crash, and the message is written so an operator can tell.

## Running one by hand

A schedule is the one thing you cannot test by waiting for it.

```
orpheus scheduled                       # list them, and what this install can run
orpheus scheduled verify                # run it here, now
orpheus scheduled verify --config batch=5
orpheus scheduled calendar-digest --json
```

Same chunks, same cloud refusal, same machine actor, same non-zero exit on a
finding. A read-only task opens the store read-only, so running the nightly
verify by hand takes no lock and applies no migration — it is safe against a
live server in a way `orpheus scheduled wiki-propose` is not.

## What was measured

Run against a live Datasette with the real scheduler, on a two-document store:

- Four tasks declared in config, all four registered as `orpheus-<name>` with
  the handler ref `orpheus_cron:<name>`; an unknown fifth logged and skipped.
- All four triggered through `/-/api/cron/tasks/<name>/trigger` and recorded
  `success` (3–20 ms).
- `wiki-propose` created two entity pages, both `unconfirmed`, both
  `created_by` the `orpheus-scheduled` actor, which was created with
  `is_admin = 0`.
- `search-index` built `document_pages_fts` and `provenance_fts` with their six
  triggers.
- One stored original was then overwritten and `verify` re-triggered: recorded
  `error`, message *"1 of 2 originals cannot be served: 1 altered. Every
  excerpt taken from them is now unverifiable."*

## Why it is an extra rather than a dependency

`datasette-cron` is `0.0.1a2` — an alpha.
[The ecosystem verdicts](datasette-ecosystem.md) set an adoption policy, and
taking an alpha as a hard dependency would contradict it. Without it installed,
`plugins/orpheus_cron.py` registers nothing and the rest of Orpheus is
unaffected; `orpheus scheduled run` still runs every task by hand.

Execution is **at-least-once**, which all four tolerate: `verify` and
`search-index` are idempotent by construction, `calendar-digest` writes
nothing, and `wiki-propose` attaches a repeated group to the page that already
exists rather than minting a second one.
