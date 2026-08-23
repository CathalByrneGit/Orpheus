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
RUN pip install --no-cache-dir '.[server]'

COPY plugins/ plugins/
COPY templates/ templates/

ENV ORPHEUS_DB=/data/orpheus.sqlite \
    ORPHEUS_STORAGE=/data/storage

VOLUME ["/data"]
EXPOSE 8001

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
