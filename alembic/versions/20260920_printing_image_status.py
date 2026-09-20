"""Ajoute scryfall_card_printings.image_status

POURQUOI
Scryfall sert une image pour TOUTE impression, y compris celles dont il ne
possède aucun scan : dans ce cas l'URL répond `200` avec un carton « Localized
Image Not Available ». Les colonnes `image_small` / `image_normal` /
`image_large` sont renseignées exactement pareil dans les deux cas, et l'octet
reçu est un vrai JPEG — vérifié le 2026-09-20 sur Misdirection (MMQ #87) :
l'impression française répond `200`, 67 Ko, `image/jpeg`, sans redirection, et
l'API Scryfall la déclare `image_status = "placeholder"` quand l'anglaise est en
`highres_scan`.

Autrement dit, **aucune donnée du catalogue ne permettait de distinguer un scan
d'un carton**. Un consommateur qui préfère l'impression d'une langue donnée — la
vitrine « Cartes recherchées » de RELIC-Trade affiche le visuel dans la langue de
l'interface — affichait le carton à la place de la carte, sans moyen de s'en
apercevoir : ni `404` à intercepter, ni champ à tester.

CE QUE LA COLONNE CHANGE
Elle porte la qualité déclarée par Scryfall (`highres_scan`, `lowres`,
`placeholder`, `missing`). Un consommateur peut alors écarter les impressions
sans visuel réel et replier sur une autre langue, au lieu de choisir à l'aveugle.

FORME
`ADD COLUMN` NULLable : opération de métadonnée, instantanée, sans réécriture des
impressions existantes. Aucun index — la colonne se lit sur des lignes déjà
sélectionnées par `(set_code, collector_number)`, jamais comme critère d'entrée.

⚠️ La colonne reste **NULL jusqu'au prochain import Scryfall**, qui la remplit
pour toutes les impressions. Les consommateurs doivent donc traiter `NULL` comme
« qualité inconnue » et se comporter comme avant — sinon, entre cette migration
et le réimport, ils écarteraient la totalité du catalogue.

Revision ID: 20260920_printing_image_status
Revises: 20260919_tagger_checked_at
Create Date: 2026-09-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260920_printing_image_status"
down_revision: Union[str, Sequence[str], None] = "20260919_tagger_checked_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "scryfall_card_printings"
COLUMN_NAME = "image_status"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {COLUMN_NAME} VARCHAR(20)"
    )


def downgrade() -> None:
    # Aucune donnée métier perdue : la colonne est repeuplée par le prochain
    # import Scryfall, qui la lit dans le bulk.
    op.execute(f"ALTER TABLE {TABLE_NAME} DROP COLUMN IF EXISTS {COLUMN_NAME}")
