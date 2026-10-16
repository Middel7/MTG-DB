"""Ajoute scryfall_card_printings.finishes

POURQUOI
Rien dans le catalogue ne disait en quelles finitions une impression existe
reellement. RELIC-Trade proposait donc « Foil / Non-foil » pour toute carte, et
affichait un prix de rachat pour des versions qui n'ont jamais ete imprimees.

Scryfall publie la reponse dans le champ `finishes` du bulk « all_cards » : une
liste parmi "nonfoil", "foil", "etched". Releve sur le bulk du 15/10/2026, les
60 000 premiers objets, 0 sans le champ :

    ['nonfoil', 'foil']             39 822
    ['nonfoil']                     17 712
    ['foil']                         1 856
    ['etched']                         498
    ['nonfoil', 'foil', 'etched']      106
    ['nonfoil', 'etched']                5
    ['foil', 'etched']                   1

Les booleens `foil` / `nonfoil` de l'API sont deprecies et ne sont pas repris :
ils ne savent pas exprimer « etched », et une carte uniquement etched y apparait
comme disponible en foil.

FORME
`ADD COLUMN` NULLable : operation de metadonnee, instantanee, sans reecriture des
539 000 impressions existantes. Aucun index — la colonne se lit sur des lignes
deja selectionnees par `(set_code, collector_number)`, jamais comme critere
d'entree. Meme raisonnement que pour `image_status`.

TEXT[] plutot que VARCHAR[] : les valeurs sont un vocabulaire ferme et court,
aucune longueur maximale n'a de sens a declarer.

⚠️ La colonne reste **NULL jusqu'au remplissage**, par `scripts/backfill_finishes.py`
ou par le prochain import Scryfall complet. Les consommateurs doivent traiter
NULL comme « finitions inconnues » et se comporter comme avant — sinon, entre
cette migration et le remplissage, ils masqueraient la totalite du catalogue.

Revision ID: 20261016_printing_finishes
Revises: 20260920_vue_prix_cm
Create Date: 2026-10-16
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20261016_printing_finishes"
down_revision: Union[str, Sequence[str], None] = "20260920_vue_prix_cm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "scryfall_card_printings"
COLUMN_NAME = "finishes"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {COLUMN_NAME} TEXT[]"
    )


def downgrade() -> None:
    # Aucune donnee metier perdue : la colonne est repeuplee par le prochain
    # import Scryfall, qui la lit dans le bulk.
    op.execute(f"ALTER TABLE {TABLE_NAME} DROP COLUMN IF EXISTS {COLUMN_NAME}")
