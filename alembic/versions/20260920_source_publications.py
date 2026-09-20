"""Cree mtgdb_source_publications et la vue mtgdb_fraicheur_sources

POURQUOI
`import_runs` repond a « quand MTG-DB a-t-il ete mis a jour ». Elle ne repond
pas a « quand la source a-t-elle publie », ni a « depuis combien de temps une
version publiee attend-elle d'etre absorbee ».

L'information existait partiellement, mais inexploitable :

  - `import_runs.source_updated_at` porte la date de publication du bulk
    Scryfall — uniquement pour les versions effectivement importees ;
  - `cardmarket_import_files.last_modified` porte celle des exports Cardmarket,
    mais en TEXTE BRUT (« Sun, 20 Sep 2026 00:42:36 GMT »), donc ni triable ni
    soustrayable ;
  - rien ne tracait une publication vue mais pas encore importee, qui est
    precisement le cas ou l'on veut etre alerte.

FORME
Une ligne par VERSION publiee, pas par verification. Le pipeline passe toutes
les heures ; Scryfall publie deux fois par jour et Cardmarket une seule.
Enregistrer chaque passage produirait 24 lignes quotidiennes de bruit pour deux
informations utiles. C'est la contrainte d'unicite (source, version) qui rend
l'ecriture idempotente.

`last_seen_at` est mise a jour a CHAQUE passage, elle. Sans elle, rien ne
distingue « la source est calme » de « le cron ne tourne plus » : dans les deux
cas, aucune nouvelle ligne n'apparait. C'est le battement de coeur de la veille.

`published_at` est nullable : un HEAD qui echoue laisse le telechargement se
faire sans en-tete `Last-Modified`, et une publication sans date reste plus
utile qu'une publication non enregistree.

Le prefixe `mtgdb_` est delibere : la base de production est partagee avec
RELIC-Trade, et un nom aussi generique que `source_publications` s'y heurterait
tot ou tard. Meme raison que `mtgdb_alembic_version`.

LA VUE
`mtgdb_fraicheur_sources` donne une ligne par source : derniere verification,
derniere version publiee, derniere importee, et le retard courant. Elle unifie
deux provenances — `mtgdb_source_publications` pour les sources versionnees, et
`import_runs` pour les tags, Tagger n'ayant aucune notion de publication (API
GraphQL interrogee en direct, sans version).

Revision ID: 20260920_source_publications
Revises: 20260920_merge_branches
Create Date: 2026-09-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260920_source_publications"
down_revision: Union[str, Sequence[str], None] = "20260920_merge_branches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


VUE = """
CREATE OR REPLACE VIEW mtgdb_fraicheur_sources AS
WITH derniere_publication AS (
    SELECT DISTINCT ON (source)
           source,
           version        AS derniere_version,
           published_at   AS publiee_le,
           detected_at    AS detectee_le,
           imported_at    AS importee_le
      FROM mtgdb_source_publications
     ORDER BY source, COALESCE(published_at, detected_at) DESC, id DESC
),
derniere_verification AS (
    SELECT source, max(last_seen_at) AS verifiee_le
      FROM mtgdb_source_publications
     GROUP BY source
),
dernier_import AS (
    SELECT DISTINCT ON (source)
           source,
           version      AS derniere_version_importee,
           imported_at  AS dernier_import_le
      FROM mtgdb_source_publications
     WHERE imported_at IS NOT NULL
     ORDER BY source, imported_at DESC, id DESC
),
amont AS (
    SELECT p.source,
           v.verifiee_le,
           p.derniere_version,
           p.publiee_le,
           p.detectee_le,
           i.derniere_version_importee,
           i.dernier_import_le,
           (p.importee_le IS NOT NULL) AS a_jour
      FROM derniere_publication p
      LEFT JOIN derniere_verification v ON v.source = p.source
      LEFT JOIN dernier_import i        ON i.source = p.source
),
-- Tagger n'a pas de version publiee : l'API GraphQL est interrogee en direct.
-- Seule la date du dernier import reussi a un sens pour cette source, et un
-- run hebdomadaire ne saurait alimenter les colonnes de publication.
tagger AS (
    SELECT 'tagger'::varchar   AS source,
           max(finished_at)    AS verifiee_le,
           NULL::text          AS derniere_version,
           NULL::timestamptz   AS publiee_le,
           NULL::timestamptz   AS detectee_le,
           NULL::text          AS derniere_version_importee,
           max(finished_at)    AS dernier_import_le,
           TRUE                AS a_jour
      FROM import_runs
     WHERE source = 'tagger' AND status = 'success'
    HAVING max(finished_at) IS NOT NULL
)
SELECT source,
       verifiee_le,
       derniere_version,
       publiee_le,
       detectee_le,
       derniere_version_importee,
       dernier_import_le,
       a_jour,
       -- Depuis combien de temps la derniere version publiee attend. NULL quand
       -- elle est deja importee : il n'y a alors aucun retard a signaler.
       CASE WHEN a_jour THEN NULL
            ELSE now() - COALESCE(publiee_le, detectee_le)
       END AS retard,
       now() - verifiee_le       AS depuis_derniere_verification,
       now() - dernier_import_le AS depuis_dernier_import
  FROM (SELECT * FROM amont UNION ALL SELECT * FROM tagger) t
 ORDER BY source
"""


def upgrade() -> None:
    op.create_table(
        "mtgdb_source_publications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "version",
                            name="uq_source_publications_source_version"),
    )
    op.create_index("ix_source_publications_source_published",
                    "mtgdb_source_publications", ["source", "published_at"])
    op.execute(VUE)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS mtgdb_fraicheur_sources")
    op.drop_index("ix_source_publications_source_published",
                  table_name="mtgdb_source_publications")
    op.drop_table("mtgdb_source_publications")
