"""Retablit v_cardmarket_latest_prices_by_printing la ou elle manque

POURQUOI
La vue est censee exister depuis 20260607_cardmarket_tables, et Alembic la croit
creee partout : la base de production Render est a jour de toutes les revisions.
Elle n'y est pourtant pas. La base ne porte qu'une seule vue,
`mtgdb_fraicheur_sources`, creee le 20/09.

L'absence ne se voyait nulle part, parce que rien ne lit cette vue DANS ce
depot : c'est ManaMind qui s'en sert, sur sa propre copie du catalogue, pour
l'ecran des cartes a changer de commandant. Elle s'est manifestee le 20/09, quand
le serveur ManaMind a voulu tirer le catalogue : `pg_dump --clean` ne peut ni
supprimer ni recreer un objet que la source n'a pas, et la vue restee sur la
cible a bloque le remplacement de `scryfall_cards` — « cannot drop table because
other objects depend on it ».

CE QUE FAIT CETTE MIGRATION
Elle recree la vue, avec les noms de tables actuels : la definition d'origine
citait `cards` et `card_printings`, renommees depuis en `scryfall_*`. Les bases
ou la vue existe deja en sortent inchangees.

FORME
`CREATE OR REPLACE VIEW` plutot que DROP + CREATE : la remplacer en place ne
casse aucune dependance et ne demande pas de verrou exclusif sur les tables
sources. La clause echouerait si les colonnes differaient ; elles ne different
pas, la definition est celle relevee par `pg_get_viewdef` sur une base saine.

Le `SELECT *` du LATERAL est conserve tel quel : il fixe la ligne de prix la plus
recente du produit, et ses colonnes ne sont pas toutes reprises en sortie.

Revision ID: 20260920_vue_prix_cm
Revises: 20260920_source_publications
Create Date: 2026-09-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260920_vue_prix_cm"
down_revision: Union[str, Sequence[str], None] = "20260920_source_publications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VUE = "v_cardmarket_latest_prices_by_printing"

DEFINITION = f"""
CREATE OR REPLACE VIEW {VUE} AS
SELECT cp.id AS printing_id,
       cp.scryfall_id,
       c.name AS card_name,
       cp.set_code,
       cp.collector_number,
       cp.lang,
       cmp.id_product,
       cmp.en_name,
       cmp.expansion_name,
       cmp.number,
       pge.low_price,
       pge.trend_price,
       pge.low_price_ex_plus,
       pge.avg1,
       pge.avg7,
       pge.avg30,
       pge.foil_low,
       pge.foil_trend,
       pge.foil_avg1,
       pge.foil_avg7,
       pge.foil_avg30,
       pge.captured_at
FROM scryfall_card_printings cp
JOIN scryfall_cards c ON c.id = cp.card_id
JOIN cardmarket_products cmp ON cmp.id_product = cp.cardmarket_id
JOIN LATERAL (
    SELECT *
    FROM cardmarket_price_guide_entries
    WHERE id_product = cp.cardmarket_id
    ORDER BY captured_at DESC
    LIMIT 1
) pge ON true
"""


def upgrade() -> None:
    op.execute(DEFINITION)


def downgrade() -> None:
    # La vue precede cette migration : la supprimer ici retirerait un objet que
    # 20260607_cardmarket_tables a cree et dont ManaMind depend.
    pass
