"""Aligne le server_default de scryfall_card_prices.date sur le modèle

POURQUOI
Le modèle déclare depuis le 13/07/2026 :

    date: Mapped[date] = mapped_column(Date, nullable=False, index=True,
                                       server_default=func.current_date())

mais aucune migration ne l'a jamais créé : la migration initiale pose la colonne
sans `server_default`. Le `DEFAULT CURRENT_DATE` existait sur la base historique,
posé hors migration, et `docs/migrations.md` note explicitement que le modèle a
été aligné sur la base sans toucher à cette dernière.

Conséquence, restée invisible jusqu'ici faute de reconstruction : une base créée
par `alembic upgrade head` n'a PAS ce défaut, là où la base historique l'a. Les
deux schémas divergent, et `alembic check` échoue sur une base neuve — ce qui
rendait impossible de vérifier en continu que modèles et migrations
correspondent.

CE QUE FAIT CETTE MIGRATION
Elle pose le défaut manquant. Sur la base historique, qui l'a déjà, l'opération
est sans effet : `ALTER COLUMN … SET DEFAULT` est idempotent.

FORME
Opération de métadonnée pure : PostgreSQL ne réécrit pas la table pour un
changement de valeur par défaut, et n'applique rien aux 15 404 827 lignes
existantes. Instantané, sans verrou long.

Revision ID: 20260919_default_prix_date
Revises: 20260826_cardmarket_id_expansion
Create Date: 2026-09-19

NOTE : l'identifiant de révision doit tenir dans les 32 caractères de
`alembic_version.version_num`. `20260826_cardmarket_id_expansion` en fait
exactement 32 — la marge est nulle, et un identifiant trop long échoue au
tout dernier UPDATE, après que la migration a pourtant été exécutée.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260919_default_prix_date"
down_revision: Union[str, Sequence[str], None] = "20260826_cardmarket_id_expansion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scryfall_card_prices ALTER COLUMN date SET DEFAULT CURRENT_DATE"
    )


def downgrade() -> None:
    # Retirer le défaut ne détruit aucune donnée et ne casse aucun consommateur :
    # l'import Scryfall fournit toujours la date explicitement
    # (`_parse_price_rows` la pose dans chaque ligne). Le défaut n'est qu'un filet
    # pour les écritures manuelles.
    op.execute("ALTER TABLE scryfall_card_prices ALTER COLUMN date DROP DEFAULT")
