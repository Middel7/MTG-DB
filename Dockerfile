# Image de mise à jour de la base MTG-DB.
#
# Conçue pour être lancée en one-shot (le conteneur fait le job puis meurt) :
#     docker compose run --rm updater
#     docker compose run --rm updater python scripts/update_all.py --only tags
#
# C'est aussi l'image du Cron Job Render « catalogue quotidien » (voir
# render.yaml et docs/deploiement.md).
#
# Surtout PAS de cron à l'intérieur : le déclenchement est la responsabilité de
# l'ordonnanceur (Render, cron de l'hôte, Ofelia, CronJob Kubernetes).

FROM python:3.12-slim

# uv installe les dépendances depuis uv.lock → build reproductible
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

# Signal autoritaire lu par mtgdb.runtime.in_container() : journal sur stdout
# plutôt que dans logs/ (le disque est éphémère), et refus d'une DATABASE_URL
# locale avant la moindre écriture. MTGDB_CONTAINER=0 au lancement rétablit le
# comportement « poste de travail », utile pour reproduire un incident.
ENV MTGDB_CONTAINER=1

WORKDIR /app

# Les dépendances d'abord : cette couche n'est reconstruite que si les manifestes changent
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Utilisateur non-root ; il doit posséder data/ (bulk Scryfall téléchargé à
# chaque run — 393 Mo, sur un disque éphémère puisqu'un Cron Job Render ne peut
# pas recevoir de disque persistant).
RUN useradd --create-home --uid 1000 mtgdb \
    && mkdir -p /app/data/raw \
    && chown -R mtgdb:mtgdb /app
USER mtgdb

# CMD et non ENTRYPOINT : Render remplace le CMD par le champ `dockerCommand`
# du service, mais jamais l'ENTRYPOINT. Avec un ENTRYPOINT, render.yaml aurait
# porté un `dockerCommand: --skip tags` illisible, dont le sens dépend d'une
# ligne du Dockerfile qu'on n'a pas sous les yeux.
#
# DATABASE_URL est fourni par l'environnement (jamais de .env dans l'image).
CMD ["python", "scripts/update_all.py", "--skip", "tags"]
