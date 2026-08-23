# Schemas

`ontologySpecR.bundle.schema.json` is vendored from
[CathalByrneGit/ontologySpecR](https://github.com/CathalByrneGit/ontologySpecR),
unmodified. It is the format an Orpheus bundle is written in.

It is kept here rather than merely referenced so the test that asserts *"the
shipped bundle is a valid ontologySpecR bundle"* runs offline and pins the
version it was checked against. Re-copy it to adopt a later revision, and
expect the test to tell you what stopped fitting.

`orpheus.bundle.schema.json` is Orpheus's own, and validates only the parts
ontologySpecR deliberately leaves open: the `extensions` objects. The base
schema sets `additionalProperties: false` everywhere, so anything Orpheus needs
that the spec does not define has to live under `extensions` — which turned out
to be a good discipline rather than a constraint to work around.
