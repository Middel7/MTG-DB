# Image de mise à jour de la base MTG-DB.
#
# Conçue pour être lancée en one-shot (le conteneur fait le job puis meurt) :
#     docker compose run --rm updater
#     docker compose run --rm updater --skip tags
#
# Surtout PAS de cron à l'intérieur : le déclenchement est la responsabilité de
# l'ordonnanceur (cron de l'hôte, Ofelia, CronJob Kubernetes, Planificateur Windows).

FROM python:3.12-slim

# uv installe les dépendances depuis uv.lock → build reproductible
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /app

# Les dépendances d'abord : cette couche n'est reconstruite que si les manifestes changent
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Utilisateur non-root ; il doit posséder data/ (verrou + bulks) et logs/
RUN useradd --create-home --uid 1000 mtgdb \
    && mkdir -p /app/data/raw /app/logs \
    && chown -R mtgdb:mtgdb /app
USER mtgdb

# DATABASE_URL est fourni par l'environnement (jamais de .env dans l'image)
ENTRYPOINT ["python", "scripts/update_all.py"]
