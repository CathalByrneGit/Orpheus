"""Orpheus references inside `datasette-paper`, so ideas can cite the corpus.

**What this is for.** A reviewer working through a corpus has thoughts that are
not findings: a half-formed argument, a note to come back to, a brief for
somebody else. Those have had nowhere to live. `datasette-paper` is a mature
collaborative editor and gives them a room; what it cannot do on its own is
point at anything in the store, because its wiki links go paper-to-paper and it
has never heard of an `ent_`.

That is what this file adds, and it is one hook wide. `paper_embed_provider`
lets a plugin claim a ref namespace and hand paper a JS bundle that renders it.
Orpheus claims `/-/orpheus/`, so **pasting an Orpheus URL into a paper turns it
into a live reference** — no new syntax, and the URL somebody already has open
in another tab is the thing that works.

**Why a reference and not a link.** A link is a string that was true once. A
reference is resolved against the reader, now:

- It carries the **review state**. An unconfirmed machine reading quoted into a
  memo reads exactly like a checked fact unless something says otherwise, and
  that is the failure this project exists to prevent. The pill says `unchecked`
  or `in dispute` in the label itself, not in a tooltip nobody opens.
- It is resolved **per viewer**. Papers live in Datasette's internal database
  where `auth.can()` never runs, so two people opening the same memo see
  different things — and "you may not see this" and "this does not exist"
  answer identically, because telling them apart enumerates the corpus.
- It **follows the store**. A merged page resolves to its survivor and says so;
  a redacted document resolves to a tombstone rather than to what it used to
  say.

`orpheus/refs.py` holds all of that; this file is the wiring, and
`plugins/paper/orpheus-embed.js` is the browser half.

**What this does not do, and it matters.** Papers are stored in Datasette's
internal database. They are outside `edit_history`, outside `orpheus export`,
and outside the reach of `redact.py` — so prose a person *typed* quoting a
document is not removed when that document is redacted. The reference pill
breaks correctly, because it is resolved live; the paragraph around it does
not. `docs/idea-space.md` says so plainly, because it is the kind of thing a
deployment has to know before it starts keeping notes rather than after.

Optional, like the agent and cron plugins: without `datasette-paper` installed
nothing here registers.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from datasette import hookimpl
from datasette.utils.asgi import Response

try:  # optional: `pip install 'orpheus[paper]'`
    import datasette_paper  # noqa: F401
    HAVE_PAPER = True
except ImportError:  # pragma: no cover - exercised by not installing the extra
    HAVE_PAPER = False

logger = logging.getLogger("orpheus.paper")

# Hand-written, not built. See the module docstring in the bundle itself.
ASSETS = Path(__file__).parent / "paper"

# Where the bundle is served. A stable path rather than a content-hashed one,
# which is why the cache header below is short: the Vite bundle can be
# `immutable` because its filename changes when it does, and this one cannot.
BUNDLE_URL = "/-/orpheus/paper/orpheus-embed.js"

# The namespace this provider claims. Everything Orpheus serves lives under it,
# which is what makes pasting a URL work: paper stores the pasted path as the
# ref and can tell it is ours before our bundle has loaded.
REF_PREFIXES = ["/-/orpheus/"]

# Must equal the `kind` the bundle's default export declares. Written out in
# both places because paper matches them at registration time and a silent
# mismatch is a provider that is never asked to render anything.
KIND = "orpheus-ref"


@hookimpl
def register_routes():
    """The bundle, and nothing else.

    Registered whether or not `datasette-paper` is installed: the file is
    static and harmless, and a route that appears and disappears with an
    optional dependency is a 404 that depends on the install rather than on
    the request.
    """
    return [(r"^/-/orpheus/paper/(?P<path>[^/]+)$", paper_asset)]


async def paper_asset(datasette, request):
    """Serve one file from `plugins/paper/`.

    The path is resolved and then checked to be inside the directory, the same
    test `static_asset` uses in the main plugin: a prefix test on the raw
    string would pass `../../etc/passwd` and follow a symlink out of the tree.
    """
    target = (ASSETS / request.url_vars["path"]).resolve()
    try:
        target.relative_to(ASSETS.resolve())
    except ValueError:
        return Response.text("Not found.", status=404)
    if not target.is_file():
        return Response.text("Not found.", status=404)

    content_type = (mimetypes.guess_type(target.name)[0]
                    or "application/octet-stream")
    if target.suffix == ".js":
        # Some servers still guess `text/plain` for .js, and a module served as
        # text/plain is refused by the browser rather than run.
        content_type = "text/javascript"
    return Response(
        target.read_bytes(),
        content_type=content_type,
        # Short, because the URL is stable. An `immutable` year here would mean
        # a fixed bundle nobody receives until they clear their cache.
        headers={"cache-control": "public, max-age=300"})


class OrpheusEmbedProvider:
    """What paper needs to know before it loads the bundle.

    Deliberately inert: it describes, it does not resolve. Resolution happens
    in the browser against the viewer's own cookie, which is what makes the
    per-viewer permission rule real rather than a claim this process makes
    about somebody else's session.
    """

    kind = KIND
    label = "Orpheus"
    ref_prefixes = REF_PREFIXES
    sources = [{"id": "orpheus-ref", "label": "Orpheus", "icon": "diagram-3"}]

    def frontend_assets(self, datasette):
        return {"js": [datasette.urls.path(BUNDLE_URL)]}


if HAVE_PAPER:
    @hookimpl
    def paper_embed_provider(datasette):
        return OrpheusEmbedProvider()
