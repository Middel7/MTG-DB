"""
Surveillance des séquences `integer`.

Pourquoi ce module existe
-------------------------
Les colonnes `id` du catalogue Scryfall sont des `serial`, donc des `integer`
plafonnés à 2 147 483 647. Ce plafond n'a rien d'abstrait : à l'atteindre,
`nextval()` lève et **toute insertion s'arrête** sur la table concernée. Il n'y a
pas de dégradation progressive, pas d'avertissement — cela fonctionne, puis cela
ne fonctionne plus.

Le piège est que le pourcentage consommé ne dit rien de l'urgence. Au 19/09/2026,
`card_printings_id_seq` était à 1,9 % de son plafond, ce qui semble confortable ;
mais elle avançait de plus d'un million par jour, pour 14 696 insertions réelles,
parce qu'`INSERT … ON CONFLICT` brûle un identifiant par ligne PROPOSÉE. C'est la
pente qui compte, jamais le niveau.

La cause a été corrigée dans `mtgdb.scryfall.upserts`. Ce module sert à voir
si elle le reste : une régression y serait invisible autrement, et ne se
manifesterait que des années plus tard.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

log = logging.getLogger("mtgdb.db.sequences")

PLAFOND_INT4 = 2_147_483_647

# Part du plafond au-delà de laquelle on alerte. 25 % : assez tôt pour planifier
# une migration `bigint` sans urgence — elle réécrit les tables et demande une
# fenêtre —, assez tard pour ne pas crier au loup pendant des années.
SEUIL_ALERTE = 0.25


@dataclass(frozen=True)
class EtatSequence:
    nom: str
    valeur: int
    lignes: int

    @property
    def part_du_plafond(self) -> float:
        return self.valeur / PLAFOND_INT4

    @property
    def ratio(self) -> float:
        """Combien de fois la séquence dépasse le nombre de lignes réelles.

        Au-dessus de 2, elle avance sans que des lignes n'apparaissent : upserts,
        rollbacks, ou TRUNCATE qui ne la remet pas à zéro.
        """
        return self.valeur / self.lignes if self.lignes else float("inf")


def etat_des_sequences(session: Session) -> list[EtatSequence]:
    """
    Relève les séquences `integer` du schéma courant, les plus avancées d'abord.

    La séquence est reliée à sa colonne par `pg_depend`, et non par une
    comparaison de noms. Un `LIKE '%' || sequencename || '%'` sur
    `column_default` paraît plus simple mais apparie à tort : `cards_id_seq` est
    contenu dans `deck_cards_id_seq`, ce qui produit des doublons et des ratios
    absurdes. `deptype = 'a'` désigne exactement le lien créé par un `serial`.

    `reltuples` est l'estimation de l'analyseur, pas un `count(*)` : suffisant
    pour un ordre de grandeur, et sans coût sur des tables de plusieurs millions
    de lignes.
    """
    lignes = session.execute(sa_text("""
        SELECT sequence.relname                       AS nom,
               COALESCE(donnees.last_value, 0)        AS valeur,
               GREATEST(COALESCE(porteuse.reltuples, 0)::bigint, 0) AS lignes
          FROM pg_class sequence
          JOIN pg_depend lien
            ON lien.objid = sequence.oid AND lien.deptype = 'a'
          JOIN pg_class porteuse
            ON porteuse.oid = lien.refobjid
          JOIN pg_attribute colonne
            ON colonne.attrelid = porteuse.oid
           AND colonne.attnum = lien.refobjsubid
          JOIN pg_namespace espace
            ON espace.oid = sequence.relnamespace
          LEFT JOIN pg_sequences donnees
            ON donnees.schemaname = espace.nspname
           AND donnees.sequencename = sequence.relname
         WHERE sequence.relkind = 'S'
           AND espace.nspname = 'public'
           AND colonne.atttypid = 'integer'::regtype
         ORDER BY valeur DESC
    """)).all()
    return [EtatSequence(nom, valeur, nb) for nom, valeur, nb in lignes]


def journaliser(session: Session, seuil: float = SEUIL_ALERTE) -> None:
    """
    Trace les séquences les plus avancées, et alerte au-delà du seuil.

    Appelé en fin de run : trois lignes de journal suffisent à rendre visible une
    dérive qui, sinon, ne se découvre que le jour où elle bloque les écritures.
    """
    try:
        sequences = etat_des_sequences(session)
    except Exception as exc:  # noqa: BLE001 — un relevé ne doit jamais tuer un run
        log.debug("Relevé des séquences impossible : %s", exc)
        return

    if not sequences:
        return

    log.info("")
    log.info("  Séquences integer les plus avancées")
    for sequence in sequences[:3]:
        log.info(
            f"    {sequence.nom:38} {sequence.valeur:>13,}  "
            f"{sequence.part_du_plafond:6.2%} du plafond"
        )

    for sequence in sequences:
        if sequence.part_du_plafond >= seuil:
            log.warning(
                f"    {sequence.nom} a consommé {sequence.part_du_plafond:.1%} du plafond "
                f"integer. Au-delà, plus aucune insertion n'est possible sur cette "
                f"table : prévoir le passage en bigint (réécriture + fenêtre)."
            )
