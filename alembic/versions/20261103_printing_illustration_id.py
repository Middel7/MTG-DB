"""Ajoute scryfall_card_printings.illustration_id et son index

POURQUOI
ManaMind veut regrouper les editions d'une carte qui partagent la MEME
illustration. Aucune colonne existante ne le permet :

  - `oracle_id` regroupe toutes les impressions d'une carte, y compris les
    rééditions entierement redessinees — c'est trop large ;
  - `(set_code, collector_number)` separe les langues d'une meme version —
    c'est trop etroit ;
  - le nom de l'artiste regroupe des illustrations differentes du meme auteur.

Scryfall publie `illustration_id`, un UUID porte par le VISUEL lui-meme : deux
impressions qui reutilisent la meme image le partagent, quelles que soient leur
edition, leur langue ou leur finition.

FORME
Type `uuid` natif plutot que String(36) comme `scryfall_id` et `oracle_id`. La
colonne porte un index, et 16 octets contre 37 divisent sa taille par deux sur
548 000 lignes. Cote Python, `as_uuid=False` rend une CHAINE, donc le meme type
de valeur que les deux autres identifiants : un consommateur qui compare a une
chaine litterale n'a rien a changer.

L'INDEX, ET POURQUOI « CONCURRENTLY »
`docs/migrations.md` l'impose pour toute grosse table : un `CREATE INDEX`
ordinaire verrouille la table en ecriture le temps de sa construction. Le cron
de production passe TOUTES LES HEURES, et un import en cours serait bloque.

`CREATE INDEX CONCURRENTLY` n'est pas transactionnel : il s'execute dans un
`autocommit_block()` et laisse un index INVALID derriere lui en cas d'echec.
D'ou la verification de `pg_index.indisvalid` avant de (re)construire, qui rend
la migration rejouable. Meme forme que `20260824_printed_name_trgm`.

Le nom de l'index est celui que SQLAlchemy genere pour `index=True` sur cette
colonne. C'est la regle du depot : tout index cree en migration doit l'etre
aussi dans le modele, avec le MEME nom, sans quoi `--autogenerate` proposerait
de le supprimer a chaque generation.

⚠️ La colonne reste **NULL jusqu'au premier import**, qui la remplit pour toutes
les impressions presentes dans le bulk. 0,16 % des objets n'en portent aucun,
meme en repliant sur `card_faces[0]` : pour ceux-la, NULL veut dire « inconnu ».

Revision ID: 20261103_illustration_id
Revises: 20261016_printing_finishes
Create Date: 2026-11-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261103_illustration_id"
down_revision: Union[str, Sequence[str], None] = "20261016_printing_finishes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "scryfall_card_printings"
COLUMN_NAME = "illustration_id"
INDEX_NAME = "ix_scryfall_card_printings_illustration_id"


def _index_state(conn: sa.engine.Connection) -> str | None:
    """'valid', 'invalid', ou None si l'index n'existe pas."""
    indisvalid = conn.execute(
        sa.text(
            """
            SELECT i.indisvalid
              FROM pg_class c
              JOIN pg_index i ON i.indexrelid = c.oid
             WHERE c.relname = :name
            """
        ),
        {"name": INDEX_NAME},
    ).scalar()
    if indisvalid is None:
        return None
    return "valid" if indisvalid else "invalid"


def upgrade() -> None:
    # ADD COLUMN d'abord, dans la transaction : operation de metadonnee,
    # instantanee, sans reecriture des lignes existantes.
    op.execute(
        f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {COLUMN_NAME} uuid"
    )

    # autocommit_block() valide la transaction courante en entrant : la colonne
    # existe donc avant que l'index ne la reference.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        state = _index_state(conn)

        if state == "valid":
            print(f"  {INDEX_NAME} deja present et valide — rien a faire.")
            return

        if state == "invalid":
            # Residu d'une creation interrompue : inutilisable, il faut le
            # reprendre a zero.
            print(f"  {INDEX_NAME} present mais INVALID — reconstruction.")
            conn.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))

        conn.execute(
            sa.text(
                f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
                f"ON {TABLE_NAME} ({COLUMN_NAME})"
            )
        )


def downgrade() -> None:
    # L'index part avec la colonne ; le DROP INDEX explicite reste pour le cas
    # ou une execution precedente aurait laisse un index sans colonne.
    with op.get_context().autocommit_block():
        op.get_bind().execute(
            sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
        )
    # Aucune donnee metier perdue : la colonne est repeuplee par le prochain
    # import Scryfall, qui la lit dans le bulk.
    op.execute(f"ALTER TABLE {TABLE_NAME} DROP COLUMN IF EXISTS {COLUMN_NAME}")
