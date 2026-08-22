# Datasette plugin

`orpheus_datasette.py` is the Orpheus UI: an upload page and per-row review
actions inside Datasette.

It is a **client over the HTTP API**, never a second writer and never a model
caller — see [../docs/deployment.md](../docs/deployment.md#the-datasette-ui-plugin)
for why that matters and how it is verified.

```bash
ORPHEUS_API_TOKEN=$TOKEN datasette serve data/orpheus.sqlite \
  --metadata inst/datasette/metadata.yml \
  --config   inst/datasette/datasette.yml \
  --plugins-dir plugins --template-dir templates --port 8001
```

Templates live in `../templates/`. Its settings are the `plugins` block of the
generated `datasette.yml` — an API URL (overridable with `ORPHEUS_API_URL`), an
upload size ceiling, and a token, per actor where possible so amendments name
the real person.

Browser file upload needs **Datasette 1.0a32 or newer**; on an older server the
page degrades to the server-side path field.
