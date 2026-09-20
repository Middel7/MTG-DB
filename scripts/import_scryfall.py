#!/usr/bin/env python3
"""
Import des cartes Magic: The Gathering depuis Scryfall bulk data vers PostgreSQL.

Ce fichier n'est plus qu'un POINT D'ENTRÉE : il lit des options, décide quoi
faire, et appelle la bibliothèque. Tout le traitement vit dans `mtgdb.scryfall` :

    mtgdb.scryfall.parsers    le JSON, et rien d'autre — fonctions pures
    mtgdb.scryfall.upserts    PostgreSQL, une Session en paramètre
    mtgdb.scryfall.bulk       le réseau : métadonnées, téléchargement, idempotence
    mtgdb.scryfall.pipeline   l'enchaînement : lots, cache, comptage

Flux :
  1. GET https://api.scryfall.com/bulk-data  → jsonl_download_uri du fichier all_cards
  2. Purge des anciens bulks, puis téléchargement → data/raw/scryfall/<filename>
  3. GET https://api.scryfall.com/sets       → upsert dans mtg_sets (FK obligatoire)
  4. Parsing streaming JSONL gzippé → lots de 500 lignes :
       cards / card_faces / card_printings / card_prices
  5. Mise à jour import_runs (début, fin, compteurs, erreurs)

Codes de sortie :
  0  import terminé sans perte
  1  au moins une carte perdue (run 'partial'), ou erreur fatale

Usage :
  python scripts/import_scryfall.py
  python scripts/import_scryfall.py --force      # retélécharge même si fichier existant
  python scripts/import_scryfall.py --dry-run    # parse et compte sans toucher la base
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import text as sa_text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.db.engine import SessionLocal, check_connection, engine
from mtgdb.db.retry import retry_transient
from mtgdb.db.publications import (
    SOURCE_SCRYFALL_BULK,
    enregistrer_publication,
    marquer_publication_importee,
)
from mtgdb.db.runs import finaliser_run, marquer_runs_orphelins, ouvrir_run
from mtgdb.db.sequences import journaliser as journaliser_sequences
from mtgdb.rawfiles import purge_old_files
from mtgdb.runtime import env_flag
from mtgdb.scryfall.bulk import (
    HTTP_HEADERS,
    bulk_already_imported,
    download_bulk_file,
    fetch_bulk_metadata,
    import_sets,
)
from mtgdb.scryfall.parsers import iter_bulk_cards
from mtgdb.scryfall.pipeline import import_cards, safe_rollback
from mtgdb.scryfall.upserts import propagate_cardmarket_ids, propagate_tcgplayer_id_en

RAW_DIR = ROOT / "data" / "raw" / "scryfall"
SOURCE = "scryfall"

# ──────────────────────────────────────────────────────────────────────────────
# SKIP_SCRYFALL_PRICES : ne pas alimenter scryfall_card_prices.
#
# À activer sur les bases où PERSONNE ne lit cette table — la production
# RELIC-Trade, typiquement. Elle y représente 61,6 Mo/jour, soit 46 % de la
# croissance, pour des lignes que rien ne consulte.
#
# À laisser DÉSACTIVÉE en local : ManaMind_AI lit bien cette table
# (routers/collection.py, join sur CardPrice pour un MIN(price) en euros).
# La couper en local casserait son affichage de prix.
#
# Pourquoi une variable d'environnement et non une détection d'URL : un
# basculement automatique sur la forme de DATABASE_URL est un piège. L'URL de
# l'hébergeur change, ou quelqu'un pointe le local vers le cloud pour un test,
# et le comportement bascule sans que personne ne l'ait demandé. Une variable
# explicite se lit dans la config et se cherche au grep.
#
# Elle est lue ICI, au point d'entrée, et passée au pipeline en paramètre : une
# bibliothèque qui interroge l'environnement au milieu de son travail ne se teste
# pas.
#
# ⚠️ Ne supprime RIEN : les lignes déjà présentes restent. On cesse d'écrire,
# on ne nettoie pas.
NOM_FLAG_PRIX = "SKIP_SCRYFALL_PRICES"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("import_scryfall")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importe les cartes MTG depuis Scryfall bulk data vers PostgreSQL."
    )
    parser.add_argument("--force", action="store_true",
                        help="Retélécharge et réimporte même si ce bulk a déjà été importé.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse sans insérer en base.")
    parser.add_argument("--keep-bulks", type=int, default=1, metavar="N",
                        help="Nombre de fichiers bulk à conserver sur disque (défaut : 1).")
    parser.add_argument("--no-purge", action="store_true",
                        help="Ne supprime pas les anciens fichiers bulk.")
    args = parser.parse_args()

    ecrire_prix = not env_flag(NOM_FLAG_PRIX)

    if not args.dry_run and not check_connection():
        log.error("Connexion PostgreSQL impossible. Vérifier DATABASE_URL dans .env.")
        sys.exit(1)

    with httpx.Client(
        timeout=httpx.Timeout(30.0, read=300.0),
        headers=HTTP_HEADERS,
        follow_redirects=True,
    ) as client:

        log.info("Récupération des métadonnées Scryfall...")
        try:
            download_uri, filename, source_updated_at = fetch_bulk_metadata(client)
        except Exception as exc:
            log.error(f"Impossible de contacter Scryfall : {exc}")
            sys.exit(1)

        log.info(f"  Source   : {filename}")
        log.info(f"  Scryfall : mis à jour le {source_updated_at.strftime('%Y-%m-%d %H:%M UTC')}")

        # La publication est notée AVANT tout test d'idempotence, donc même
        # lorsqu'on n'importera rien. C'est ce qui permet de répondre à « depuis
        # combien de temps cette version attend-elle ? » plutôt qu'au seul
        # « qu'avons-nous importé ? ».
        if not args.dry_run:
            enregistrer_publication(SOURCE_SCRYFALL_BULK, filename, source_updated_at)

        # Ce bulk est-il déjà en base ? Si oui, inutile de le retélécharger ni de le
        # re-parser : on sort en succès. C'est ce qui rend les runs planifiés répétés
        # (toutes les heures) quasi gratuits quand Scryfall n'a rien republié.
        if not args.dry_run and not args.force:
            with SessionLocal() as session:
                if bulk_already_imported(session, download_uri):
                    log.info("Ce bulk a déjà été importé avec succès — rien à faire.")
                    log.info("  (--force pour réimporter malgré tout)")
                    # Le suivi doit refléter que cette version est absorbée, même
                    # si c'est un run antérieur qui l'a fait : sans cela, la
                    # première exécution suivant la création de la table verrait
                    # un retard imaginaire sur une version déjà en base.
                    marquer_publication_importee(SOURCE_SCRYFALL_BULK, filename)
                    if not args.no_purge:
                        purge_old_files(RAW_DIR, keep=args.keep_bulks, current=filename, logger=log)
                    return

        dest = RAW_DIR / filename

        # Purge AVANT le téléchargement : les anciens bulks sont déjà importés, les garder
        # pendant le parsing (5 à 18 min) ferait cohabiter 2×2,6 Go sur le disque pour rien.
        # `filename` est réservé dans le budget `keep`, donc un téléchargement partiel déjà
        # présent survit et reste réutilisable.
        if not args.no_purge and not args.dry_run:
            log.info("Purge des anciens fichiers bulk...")
            purge_old_files(RAW_DIR, keep=args.keep_bulks, current=filename, logger=log)

        if dest.exists() and not args.force:
            log.info(f"Fichier déjà présent ({dest.stat().st_size / 1_048_576:.0f} Mo). "
                     f"Utilise --force pour retélécharger.")
        else:
            log.info(f"Téléchargement vers {dest} ...")
            download_bulk_file(client, download_uri, dest)

        if args.dry_run:
            log.info("[DRY-RUN] Comptage sans insertion...")
            count = 0
            for _ in iter_bulk_cards(dest):
                count += 1
                if count % 50_000 == 0:
                    log.info(f"  {count:,} objets parsés...")
            log.info(f"[DRY-RUN] Total : {count:,} objets.")
            return

        with SessionLocal() as session:
            orphelins = marquer_runs_orphelins(session, source=SOURCE)
            if orphelins:
                log.warning(
                    f"{orphelins} run(s) precedent(s) restes 'running' marques 'failed' "
                    f"— processus interrompu sans finalisation."
                )

            run_id = ouvrir_run(session, SOURCE, source_file=download_uri)
            # `source_updated_at` n'appartient qu'à Scryfall : le module générique
            # `mtgdb.db.runs` n'a pas à le connaître.
            session.execute(
                sa_text("UPDATE import_runs SET source_updated_at = :maj WHERE id = :id"),
                {"maj": source_updated_at, "id": run_id})
            session.commit()
            log.info(f"Import run #{run_id} démarré.")

            started_at = datetime.now(timezone.utc)
            try:
                log.info("Import des éditions...")
                n_sets = import_sets(client, session)
                log.info(f"  {n_sets} éditions importées/mises à jour.")

                log.info("Import des cartes (streaming, lots de 500)...")
                cards_n, printings_n, errors_n = import_cards(
                    dest, session, ecrire_prix=ecrire_prix)

                log.info("Propagation des cardmarket_id aux impressions non-anglaises...")
                propagated = retry_transient(
                    lambda: propagate_cardmarket_ids(session),
                    description="propagation cardmarket_id",
                    on_retry=lambda: safe_rollback(session),
                )
                log.info(f"  {propagated:,} impression(s) mise(s) à jour.")

                log.info("Propagation des tcgplayer_id_en (ID anglais vers toutes les langues)...")
                propagated_tcg = retry_transient(
                    lambda: propagate_tcgplayer_id_en(session),
                    description="propagation tcgplayer_id_en",
                    on_retry=lambda: safe_rollback(session),
                )
                log.info(f"  {propagated_tcg:,} impression(s) mise(s) à jour.")

                elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
                # 'partial' et non 'success' des qu'une carte a ete perdue : un run
                # ou 300 000 cartes ont echoue n'est pas un succes. Consequences
                # voulues : bulk_already_imported() ne le voit pas, donc le prochain
                # run reprend ce bulk ; et la supervision cote RELIC-Trade, qui
                # compte les 'success', signale le decrochage.
                statut_final = "success" if errors_n == 0 else "partial"
                finaliser_run(SessionLocal, run_id, statut_final, cards=cards_n,
                              printings=printings_n, errors=errors_n,
                              on_retry=lambda: engine.dispose() if engine is not None else None)

                # Uniquement sur un vrai succès : une version partiellement
                # importée n'est pas absorbée, et le suivi doit continuer à la
                # signaler en attente jusqu'à ce qu'un run la reprenne.
                if statut_final == "success":
                    marquer_publication_importee(SOURCE_SCRYFALL_BULK, filename)

                if errors_n:
                    log.warning(
                        f"Run marque 'partial' : {errors_n} carte(s) perdue(s). "
                        f"Le prochain run reprendra ce bulk."
                    )

                log.info("")
                log.info("=" * 47)
                log.info(f"  Cartes          : {cards_n:>10,}")
                log.info(f"  Impressions     : {printings_n:>10,}")
                log.info(f"  CM id propagés  : {propagated:>10,}")
                log.info(f"  TCG id_en prop. : {propagated_tcg:>10,}")
                log.info(f"  Éditions        : {n_sets:>10,}")
                log.info(f"  Erreurs         : {errors_n:>10}")
                log.info(f"  Durée           : {elapsed:>9}s")
                log.info("=" * 47)

                # Les colonnes `id` sont des integer. Tracer leur consommation à
                # chaque run est le seul moyen de voir revenir une régression sur
                # les upserts : elle ne se manifesterait, sinon, que le jour où
                # une séquence bute sur son plafond et bloque les insertions.
                journaliser_sequences(session)

                # Un run 'partial' doit SORTIR en echec. Le marquer en base ne
                # suffisait pas : le processus rendait 0, `update_all.py` affichait
                # « OK » et la tache planifiee remontait LastTaskResult=0. Un run
                # ayant perdu 300 000 cartes produisait donc exactement le meme
                # signal d'exploitation qu'un run parfait.
                if errors_n:
                    sys.exit(1)

            except Exception as exc:
                elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
                log.error(f"Erreur fatale après {elapsed}s : {exc}", exc_info=True)
                safe_rollback(session)
                finaliser_run(SessionLocal, run_id, "failed", error_message=str(exc)[:2000])
                sys.exit(1)


if __name__ == "__main__":
    main()
