"""The wiring between `datasette-paper` and Orpheus references.

Not what a reference says — that is `test_refs.py`. What this holds is the
contract with somebody else's plugin, which is the part that breaks silently:
a `kind` that does not match the bundle's is a provider paper never asks to
render, and a `ref_prefixes` that does not cover the URLs people actually paste
is a bundle that never loads. Neither raises; both just quietly do nothing.

The JavaScript half is checked here too, as text rather than by running it.
That is a deliberately modest test and it says so: it holds the two constants
that have to agree across the language boundary, and the leak rule that a
refusal never carries a label. Whether the DOM it builds looks right was
checked by hand against a live paper, and `docs/idea-space.md` records what was
seen.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", ROOT / "plugins" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load("orpheus_paper")
BUNDLE = (ROOT / "plugins" / "paper" / "orpheus-embed.js").read_text()


class FakeUrls:
    def path(self, value):
        return value


class FakeDatasette:
    urls = FakeUrls()


# -- the contract across the language boundary ------------------------------

def test_the_kind_matches_what_the_bundle_exports():
    """A mismatch is a provider paper registers and never asks to render."""
    assert f'kind: "{plugin.KIND}"' in BUNDLE


def test_the_bundle_url_the_hook_advertises_is_the_one_that_is_served():
    served = plugin.ASSETS / pathlib.Path(plugin.BUNDLE_URL).name
    assert served.is_file()
    assert plugin.OrpheusEmbedProvider().frontend_assets(FakeDatasette()) == {
        "js": [plugin.BUNDLE_URL]}


def test_the_claimed_namespace_covers_the_urls_people_will_paste():
    """The feature is "paste an Orpheus URL". A prefix that misses the pages
    somebody actually has open is a bundle that never loads."""
    assert plugin.REF_PREFIXES == ["/-/orpheus/"]
    for url in ("/-/orpheus/wiki/ent_abc", "/-/orpheus/document/doc_abc",
                "/-/orpheus/ref/inst_abc"):
        assert any(url.startswith(prefix) for prefix in plugin.REF_PREFIXES)


def test_the_bundle_claims_exactly_those_three_url_shapes():
    match = re.search(r"const REF_PATH = /(.+?)/;", BUNDLE)
    assert match, "the bundle must declare one ref pattern"
    pattern = re.compile(match.group(1))
    for url in ("/-/orpheus/wiki/ent_abc", "/-/orpheus/document/doc_abc",
                "/-/orpheus/ref/inst_9f2c"):
        assert pattern.search(url), url
    for url in ("/-/orpheus/network", "/-/cron", "/-/orpheus/wiki/ent_abc/edit",
                "/-/orpheus/ref/"):
        assert not pattern.search(url), url


# -- the hook ---------------------------------------------------------------

def test_the_provider_is_offered_only_when_paper_is_installed():
    pytest.importorskip("datasette_paper")
    provider = plugin.paper_embed_provider(FakeDatasette())
    assert provider.kind == plugin.KIND
    assert provider.ref_prefixes == plugin.REF_PREFIXES
    # `sources` is read before the bundle loads, so the picker can offer
    # Orpheus in the / menu without fetching anything.
    assert provider.sources and provider.sources[0]["id"] == "orpheus-ref"


def test_the_provider_only_describes_and_never_resolves():
    """Resolution happens in the browser against the viewer's own cookie. A
    provider that resolved here would be this process making a claim about
    somebody else's session."""
    provider = plugin.OrpheusEmbedProvider()
    assert not hasattr(provider, "resolve")
    assert not hasattr(provider, "resource_url")


def test_describing_the_provider_touches_no_store():
    # It is handed a Datasette with no databases at all and must still answer.
    assert plugin.OrpheusEmbedProvider().frontend_assets(FakeDatasette())["js"]


# -- serving the bundle -----------------------------------------------------

def test_the_bundle_route_is_registered_whether_or_not_paper_is_installed():
    paths = [pattern for pattern, _ in plugin.register_routes()]
    assert paths == [r"^/-/orpheus/paper/(?P<path>[^/]+)$"]


def _serve(path):
    import asyncio

    class Request:
        url_vars = {"path": path}

    return asyncio.run(plugin.paper_asset(FakeDatasette(), Request()))


def test_the_bundle_is_served_as_javascript():
    response = _serve("orpheus-embed.js")
    assert response.status == 200
    # A module served as text/plain is refused by the browser rather than run.
    assert response.content_type == "text/javascript"
    assert b"export default" in response.body


def test_a_path_out_of_the_directory_is_refused():
    for path in ("..%2F..%2Fetc%2Fpasswd", "../../etc/passwd", "nothing.js"):
        assert _serve(path).status == 404


# -- the leak rule, on the browser side -------------------------------------

def test_a_refusal_never_carries_a_label_in_the_bundle_either():
    """The backend withholds it; the bundle must not invent one. Both the
    denied and not_found returns are bare status objects."""
    assert '{ status: "denied" }' in BUNDLE
    assert '{ status: "not_found" }' in BUNDLE
    assert re.search(r'status:\s*"denied",\s*label', BUNDLE) is None
    assert re.search(r'status:\s*"not_found",\s*label', BUNDLE) is None


def test_a_network_failure_is_not_reported_as_an_absence():
    """"Could not reach the store" and "this does not exist" are different
    claims, and only one of them is ours to make."""
    assert '{ status: "error" }' in BUNDLE


def test_the_icon_markup_is_static_and_never_built_from_store_data():
    """Paper renders `icon` as raw HTML by contract. Interpolating anything
    from the store into it is the one way to turn that into an injection."""
    icons = re.search(r"const ICONS = \{(.+?)\n\};", BUNDLE, re.S)
    assert icons, "the bundle must declare its icons in one place"
    assert "${" not in icons.group(1)
    assert "+" not in icons.group(1)
