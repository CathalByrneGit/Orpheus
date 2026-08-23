# Deployment files

| File | Purpose |
|---|---|
| `Dockerfile` | The Orpheus image: Datasette, the plugin, and the core it imports |
| `docker-compose.yml` | Orpheus and a local model on one host |

There is one service. Datasette is the writer, so the Plumber API that used to
own the only write connection — and the read-only Datasette mount that made that
arrangement safe — are both gone.

## Standing one up

```bash
cd deploy
echo "DATASETTE_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env
docker compose up -d
```

The store does not exist yet. Create it, with an admin, before starting the
server for real:

```bash
docker compose run --rm --no-deps orpheus \
  orpheus --db /data/orpheus.sqlite init \
    --admin "Your Name" --admin-email you@example.gov \
    --config /data/config/datasette.yml \
    --storage-root /data/storage
```

`run --rm` rather than `exec`, because `init` takes the advisory writer lock and
a running server holds the database open. Against a live server it would be
refused — which is the lock doing its job, not a problem to work around.

Then bring the service up and sign in. Authentication is Datasette's, not
Orpheus's: `datasette-auth-passwords`, `datasette-auth-github` or an SSO plugin
all work, and the plugin maps whichever actor id they produce onto an Orpheus
actor through `actor_map` in the generated config:

```yaml
plugins:
  orpheus-datasette:
    actor_map:
      github|12345: act_1f2e3d...
```

Without that mapping, edits are attributed to the Datasette actor id verbatim,
which will not match a row in `actors`.

## Running a corpus through it

The CLI is the way to put a directory of documents through the pipeline. It
opens the store directly, so stop the server first:

```bash
docker compose stop orpheus
docker compose run --rm --no-deps -v /path/to/corpus:/corpus orpheus \
  orpheus --db /data/orpheus.sqlite ingest /corpus \
    --actor-id act_... --extract --storage-root /data/storage
docker compose start orpheus
```

Then read the report:

```bash
docker compose exec orpheus orpheus --db /data/orpheus.sqlite report
```

`report` opens the store read-only and takes no lock, so it runs against a live
server.

## Before anyone outside the host uses this

The port is published on `127.0.0.1` only. Put a TLS-terminating reverse proxy
in front of it. See [../docs/deployment.md](../docs/deployment.md).
