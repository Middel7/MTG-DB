"""
Configuration Alembic pour MTG-DB.
- Charge DATABASE_URL depuis .env
- Importe tous les modèles pour l'autogenerate
- Supporte les migrations online et offline
"""
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

load_dotenv(ROOT / ".env")

from mtgdb.db.base import Base  # noqa: E402
import mtgdb.db.models  # noqa: E402, F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

database_url = os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

# La base `manamind` est PARTAGÉE avec d'autres projets (ManaMind_AI, mtgtrade), qui y
# appliquent leurs propres migrations. Deux précautions en découlent :
#
# 1. VERSION_TABLE : chaque projet a besoin de sa propre table de version, sinon les
#    historiques se piétinent (ManaMind_AI utilise `alembic_version`, mtgtrade utilise
#    `mtgtrade_alembic_version`). MTG-DB a la sienne.
#
# 2. INCLUDE_OBJECT : sans ce filtre, l'autogenerate verrait les tables des autres
#    projets (users, deck_cards, card_neighbors…), les croirait supprimées puisqu'elles
#    ne sont pas dans nos modèles, et générerait des op.drop_table() dessus.
VERSION_TABLE = "mtgdb_alembic_version"

# Tables dont ManaMind_AI est propriétaire, bien que MTG-DB en expose des modèles pour
# les lire. Ses migrations y ajoutent des colonnes (tfidf, idf, tfidf_norm) que nos
# modèles ignorent : sans cette exclusion, l'autogenerate proposerait de les supprimer.
# Les modèles restent utilisables en lecture — ils sortent seulement du périmètre des
# migrations de ce dépôt.
FOREIGN_TABLES = {"deck_stat_global", "deck_stat_commander"}


def include_object(object, name, type_, reflected, compare_to):
    """N'expose à l'autogenerate que les tables dont MTG-DB est réellement propriétaire."""
    if type_ == "table":
        if name in FOREIGN_TABLES:
            return False
        # Table présente en base mais absente de nos modèles → appartient à un autre
        # projet (users, deck_cards, card_neighbors…). Ne pas proposer de la supprimer.
        if reflected and name not in target_metadata.tables:
            return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        version_table=VERSION_TABLE,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            version_table=VERSION_TABLE,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
