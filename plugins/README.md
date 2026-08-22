# Datasette plugin

`orpheus_datasette.py` is the Orpheus UI: an upload page and per-row review
actions inside Datasette.

It is a **client over the HTTP API**, never a second writer and never a model
caller — see [../docs/deployment.md](../docs/deployment.md#the-datasette-ui-plugin)
for why that matters and how it is verified.

```bash
datasette serve data/orpheus.sqlite \
  --plugins-dir plugins --template-dir templates \
  --metadata datasette.yml --port 8001
```

Templates live in `../templates/`. The only configuration is the API URL and a
token — per actor where possible, so amendments name the real person.
