# Orpheus API — the single writer.
#
# Datasette runs as a separate service against the same volume (see
# docker-compose.yml). It must not be given a writable mount of the store: the
# single-writer constraint is what makes SQLite viable here.

FROM rocker/r-ver:4.4.1

RUN apt-get update && apt-get install -y --no-install-recommends \
      poppler-utils \
      tesseract-ocr \
      libcurl4-openssl-dev libssl-dev libxml2-dev \
      zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN install2.r --error --skipinstalled \
      DBI RSQLite jsonlite digest rlang cli plumber testthat withr remotes pdftools

# The ontology stack. ontologyDiscoverR is the extraction engine; conceptR
# provides versioned rule concepts; objectSetsR backs the corpus escalation and
# has a SQL fallback if it is unavailable.
RUN R -e "remotes::install_github(c( \
      'CathalByrneGit/ontologySpecR', \
      'CathalByrneGit/ontologyDiscoverR', \
      'CathalByrneGit/conceptR', \
      'CathalByrneGit/objectSetsR'), upgrade = 'never')"

WORKDIR /app
COPY DESCRIPTION NAMESPACE ./
COPY R/ R/
COPY inst/ inst/
COPY man/ man/
RUN R CMD INSTALL .

ENV ORPHEUS_DB=/data/orpheus.sqlite \
    ORPHEUS_STORAGE=/data/storage \
    ORPHEUS_HOST=0.0.0.0 \
    ORPHEUS_PORT=8000

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD R -e "httr2::request('http://127.0.0.1:8000/health') |> httr2::req_perform()" || exit 1

CMD ["Rscript", "inst/plumber/plumb.R"]
