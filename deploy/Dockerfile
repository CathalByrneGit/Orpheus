# Orpheus: one process.
#
# Datasette is the writer and the core is a library it imports, so there is no
# second service to deploy and no second connection to reason about. The R
# implementation needed two -- a Plumber API that owned the only write
# connection, and Datasette mounted read-only beside it -- and the read-only
# mount was how the single-writer rule was enforced. Here the rule is stricter
# and does not need the filesystem to hold it up: nothing writes except through
# `orpheus` core functions, and every one of those goes through Datasette's own
# write queue.

FROM python:3.11-slim

# poppler-utils gives pdftotext and pdftoppm; tesseract is only reached for
# pages with no text layer, and only if OCR is enabled.
RUN apt-get update && apt-get install -y --no-install-recommends \
      poppler-utils \
      tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY orpheus/ orpheus/

# There is no cron daemon in this image and there should not be: it runs one
# process. A deployment that wants scheduled work builds with
# `--build-arg ORPHEUS_EXTRAS=server,cron`, which adds datasette-cron and lets
# the scheduler run inside the process that owns the database. Left out of the
# default because datasette-cron is an alpha, and because a deployment that
# schedules nothing should not carry a scheduler.
# See docs/scheduled-tasks.md.
ARG ORPHEUS_EXTRAS=server
RUN pip install --no-cache-dir ".[${ORPHEUS_EXTRAS}]"

COPY plugins/ plugins/
COPY templates/ templates/

ENV ORPHEUS_DB=/data/orpheus.sqlite \
    ORPHEUS_STORAGE=/data/storage

VOLUME ["/data"]
EXPOSE 8001

# Doubles as the traffic datasette-cron needs: its scheduler loop starts on the
# first HTTP request and only runs while requests keep arriving, so a container
# nobody visits overnight would otherwise run no scheduled tasks at all.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request; \
      urllib.request.urlopen('http://127.0.0.1:8001/-/orpheus/api/health')" || exit 1

# The filename stem must stay `orpheus`: Datasette derives the database name
# from it and matches the generated config's keys against that name.
#
# Deliberately NOT --immutable. That flag makes SQLite skip the WAL, so a live
# store reads as of the last checkpoint -- rows silently missing, no error.
CMD ["datasette", "serve", "/data/orpheus.sqlite", \
     "--metadata", "/data/config/metadata.yml", \
     "--config", "/data/config/datasette.yml", \
     "--plugins-dir", "plugins", "--template-dir", "templates", \
     "--host", "0.0.0.0", "--port", "8001", \
     "--setting", "sql_time_limit_ms", "3000", \
     "--setting", "max_returned_rows", "2000"]
