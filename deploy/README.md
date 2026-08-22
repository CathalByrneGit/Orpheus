# Deployment files

| File | Purpose |
|---|---|
| `Dockerfile` | The Orpheus API image, including the R ontology stack |
| `docker-compose.yml` | API + Datasette + Ollama on one host |

```bash
cd deploy && docker compose up -d
docker compose exec api R -e '
  library(orpheus)
  con <- orph_connect(Sys.getenv("ORPHEUS_DB"))
  a <- orph_create_actor(con, "Admin", is_admin = TRUE)
  print(orph_create_token(con, a, "bootstrap")$token)
  orph_setup_concepts(con, actor_id = a)
  orph_disconnect(con)'
```

That bootstrap step needs the API stopped, or it will be refused by the
single-writer lock — which is the lock doing its job. Stop the API first
(`docker compose stop api`), create the actor, then start it again.

Put the printed token in the environment so the UI can reach the API as that
actor, and every amendment made through the browser is attributed to them:

```bash
echo "ORPHEUS_API_TOKEN=<token>" >> .env
docker compose up -d datasette
```

Neither port is published beyond `127.0.0.1`. Put a TLS-terminating reverse
proxy in front of both before anyone outside the host uses this. See
[../docs/deployment.md](../docs/deployment.md).
