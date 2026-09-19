#!/usr/bin/env python3
"""
Import des tags oracle (ORACLE_CARD_TAG) depuis Scryfall Tagger vers scryfall_card_tags.

Flux :
  1. Récupère un token CSRF + cookies de session sur tagger.scryfall.com
  2. Pour chaque carte sans tags en base, récupère une impression (set_code + collector_number)
  3. Appelle l'API GraphQL non officielle du Tagger (cardBySet)
  4. Upsert des tags ORACLE_CARD_TAG dans scryfall_card_tags

Usage :
  python scripts/import_tagger_tags.py                  # cartes sans tags uniquement
  python scripts/import_tagger_tags.py --all            # toutes les cartes (remplace l'existant)
  python scripts/import_tagger_tags.py --limit 500      # limite à N cartes (test)
  python scripts/import_tagger_tags.py --delay 0.3      # délai entre requêtes (défaut : 0.2s)

Notes :
  - L'API GraphQL du Tagger est non officielle et peut changer sans préavis.
  - Le token CSRF est rafraîchi toutes les 200 requêtes ou sur erreur d'authentification.

Codes de sortie :
  0  l'import s'est déroulé normalement
  1  Tagger est trop souvent indisponible (voir SEUIL_ECHEC) ou la base est
     injoignable

Une carte que Tagger ne connaît pas n'est PAS une erreur : c'est une réponse
valide, et elle ne compte pas dans le seuil. Seules les pannes de transport
(timeout, HTTP 5xx, 429 répétés) comptent. Confondre les deux était le défaut
d'origine : `graphql_request()` retournait `None` dans les deux cas, le compteur
d'erreurs restait donc à zéro même quand 100 % des requêtes échouaient, et le
script sortait en succès après 40 minutes de travail perdu.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.db.engine import SessionLocal, check_connection
from mtgdb.db.models.card_tag import CardTag
from mtgdb.db.runs import finaliser_run, marquer_runs_orphelins, ouvrir_run

TAGGER_BASE = "https://tagger.scryfall.com"
GRAPHQL_URL = f"{TAGGER_BASE}/graphql"
CSRF_REFRESH_EVERY = 200

# Valeur de `import_runs.source` pour cette étape. Figée : c'est la clé sur
# laquelle la supervision interroge la fraîcheur des tags.
SOURCE = "tagger"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False),
)
log = logging.getLogger("import_tagger_tags")

FETCH_CARD_QUERY = """
query FetchCard($set: String!, $number: String!, $back: Boolean = false) {
  card: cardBySet(set: $set, number: $number, back: $back) {
    name
    taggings {
      tag {
        name
        type
      }
    }
  }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# SESSION TAGGER (CSRF)
# ══════════════════════════════════════════════════════════════════════════════

def refresh_session(client: httpx.Client) -> str:
    """Récupère un nouveau token CSRF depuis la page d'accueil du Tagger."""
    resp = client.get(f"{TAGGER_BASE}/", follow_redirects=True)
    resp.raise_for_status()
    for line in resp.text.splitlines():
        if 'csrf-token' in line and 'content=' in line:
            start = line.find('content="') + 9
            end = line.find('"', start)
            if start > 8 and end > start:
                token = line[start:end]
                log.debug("Token CSRF rafraîchi.")
                return token
    raise RuntimeError("Token CSRF introuvable dans la page Tagger.")


def graphql_request(
    client: httpx.Client,
    csrf_token: str,
    set_code: str,
    collector_number: str,
) -> Optional[list[str]]:
    """
    Liste des ORACLE_CARD_TAG de la carte.

    Retourne `None` uniquement quand Tagger répond correctement qu'il ne connaît
    pas cette carte — c'est un résultat, pas un incident.

    Lève `_TaggerIndisponible` quand la requête n'a pas abouti (timeout, HTTP non
    200, 429 répétés). Ces deux situations étaient auparavant confondues sous un
    même `return None`, ce qui rendait une panne totale de Tagger indiscernable
    d'un catalogue simplement inconnu.
    """
    payload = {
        "operationName": "FetchCard",
        "variables": {"set": set_code, "number": collector_number, "back": False},
        "query": FETCH_CARD_QUERY,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRF-Token": csrf_token,
        "Referer": f"{TAGGER_BASE}/card/{set_code}/{quote(collector_number)}",
        "Origin": TAGGER_BASE,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    for attempt in range(3):
        try:
            resp = client.post(GRAPHQL_URL, json=payload, headers=headers, timeout=15)
        except httpx.TimeoutException as exc:
            raise _TaggerIndisponible(
                f"timeout pour {set_code}/{collector_number}") from exc
        except httpx.HTTPError as exc:
            raise _TaggerIndisponible(
                f"erreur de transport pour {set_code}/{collector_number} : {exc}") from exc

        if resp.status_code == 429:
            wait = [10, 15, 20][attempt]  # 10s, 15s, 20s
            log.warning("429 Too Many Requests — attente %ds avant retry %d/3", wait, attempt + 1)
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            raise _TaggerIndisponible(
                f"HTTP {resp.status_code} pour {set_code}/{collector_number}")

        break
    else:
        raise _TaggerIndisponible(
            f"abandon apres 3 tentatives (429) pour {set_code}/{collector_number}")

    try:
        body = resp.json()
    except ValueError as exc:
        # Une page d'erreur HTML servie en 200 : Tagger est en panne, pas la carte.
        raise _TaggerIndisponible(
            f"reponse illisible pour {set_code}/{collector_number}") from exc

    # Token invalide → signal pour rafraîchir
    if not body.get("data") and body.get("message") == "invalid authenticity token":
        raise _CsrfExpired()

    card_data = (body.get("data") or {}).get("card")
    if not card_data:
        return None

    return [
        t["tag"]["name"]
        for t in (card_data.get("taggings") or [])
        if t.get("tag", {}).get("type") == "ORACLE_CARD_TAG"
    ]


class _CsrfExpired(Exception):
    """Le token CSRF n'est plus accepté : il faut en redemander un."""


class _TaggerIndisponible(Exception):
    """La requête n'a pas abouti. À distinguer d'une carte que Tagger ne connaît pas."""


# Part maximale de requêtes en échec au-delà de laquelle le run est déclaré
# raté. 20 % : Tagger renvoie ponctuellement des 5xx isolés sur un catalogue de
# plusieurs milliers de cartes, et faire échouer un run pour trois timeouts
# apprendrait surtout à ignorer l'alerte. En revanche, au-delà d'une requête sur
# cinq, ce n'est plus du bruit — c'est une panne, et le run doit le dire.
SEUIL_ECHEC = 0.20

# Délai avant de réinterroger une carte pour laquelle Tagger n'a rien produit.
# 90 jours : Tagger enrichit son catalogue au fil des sorties, mais rarement pour
# des cartes anciennes qu'il a déjà vues. Plus court ne ferait que réémettre les
# mêmes requêtes ; beaucoup plus long retarderait la prise en compte des cartes
# taguées après coup.
RECONTROLE_JOURS = 90


# ══════════════════════════════════════════════════════════════════════════════
# SÉLECTION DES CARTES À TRAITER
# ══════════════════════════════════════════════════════════════════════════════

def fetch_cards_to_process(session: Session, only_missing: bool,
                           limit: Optional[int]) -> list[tuple[int, str, str, str]]:
    """
    Retourne une liste de (card_id, card_name, set_code, collector_number).

    Choisit une impression anglaise en priorité, sinon la plus récente.

    Si `only_missing`, écarte deux populations :

      - les cartes qui ont déjà au moins un tag ;
      - celles vérifiées depuis moins de `RECONTROLE_JOURS`, même si Tagger n'a
        rien produit pour elles. Sans ce second filtre, une carte que Tagger
        connaît mais n'a taguée avec rien restait éternellement « sans tag » et
        se voyait réinterrogée à chaque run — une requête HTTP et 0,2 s de pause,
        chaque semaine, indéfiniment.

    Le recontrôle reste nécessaire : Tagger enrichit son catalogue en continu.
    Il devient simplement périodique au lieu d'être systématique.
    """
    # Sous-requête : une impression par carte (anglaise en priorité)
    # On utilise DISTINCT ON (card_id) ordonné par lang='en' DESC
    sql = text("""
        SELECT DISTINCT ON (cp.card_id)
            c.id        AS card_id,
            c.name      AS card_name,
            cp.set_code,
            cp.collector_number
        FROM scryfall_cards c
        JOIN scryfall_card_printings cp ON cp.card_id = c.id
        WHERE cp.digital = false
          AND cp.set_code NOT IN ('sld', 'ptc', 'plist')
          {missing_filter}
        ORDER BY cp.card_id, (cp.lang = 'en') DESC, cp.released_at DESC
        {limit_clause}
    """.format(
        missing_filter="""
          AND NOT EXISTS (
              SELECT 1 FROM scryfall_card_tags t WHERE t.card_id = c.id
          )
          AND (c.tagger_checked_at IS NULL
               OR c.tagger_checked_at < now() - make_interval(days => :jours))
        """ if only_missing else "",
        limit_clause=f"LIMIT {limit}" if limit else "",
    ))
    parametres = {"jours": RECONTROLE_JOURS} if only_missing else {}
    rows = session.execute(sql, parametres).fetchall()
    return [(r.card_id, r.card_name, r.set_code, r.collector_number) for r in rows]


def marquer_verifiees(session: Session, card_ids: list[int]) -> None:
    """
    Note que Tagger a répondu pour ces cartes, tags ou non.

    C'est cette trace — et non la seule présence de tags — qui permet de ne pas
    réinterroger indéfiniment une carte que Tagger ne connaît pas.
    """
    if not card_ids:
        return
    session.execute(
        text("UPDATE scryfall_cards SET tagger_checked_at = now() WHERE id = ANY(:ids)"),
        {"ids": card_ids},
    )


# ══════════════════════════════════════════════════════════════════════════════
# UPSERT DES TAGS
# ══════════════════════════════════════════════════════════════════════════════

def upsert_tags(session: Session, card_id: int, tag_names: list[str], replace: bool) -> int:
    """
    Insère les tags pour une carte. Si replace=True, supprime d'abord les anciens.
    Retourne le nombre de tags insérés.
    """
    if not tag_names:
        return 0

    if replace:
        session.execute(delete(CardTag).where(CardTag.card_id == card_id))

    rows = [{"card_id": card_id, "tag_name": name} for name in tag_names]
    stmt = pg_insert(CardTag).values(rows).on_conflict_do_nothing(
        constraint="uq_card_tag"
    )
    session.execute(stmt)
    return len(rows)


# ══════════════════════════════════════════════════════════════════════════════
# POINT D'ENTRÉE
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Import des tags Scryfall Tagger")
    parser.add_argument(
        "--all", dest="process_all", action="store_true",
        help="Traite toutes les cartes et remplace les tags existants"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limite à N cartes (utile pour les tests)"
    )
    parser.add_argument(
        "--delay", type=float, default=0.2,
        help="Délai en secondes entre chaque requête (défaut : 0.2)"
    )
    args = parser.parse_args()

    only_missing = not args.process_all

    if not check_connection():
        log.error("Impossible de se connecter à la base de données.")
        sys.exit(1)

    total_tags = 0
    errors = 0
    cartes_traitees = 0

    with SessionLocal() as session:
        orphelins = marquer_runs_orphelins(session, source=SOURCE)
        if orphelins:
            log.warning("%d run(s) tagger reste(s) 'running' marque(s) 'failed'.", orphelins)

        log.info("Récupération des cartes à traiter...")
        cards = fetch_cards_to_process(session, only_missing=only_missing, limit=args.limit)

        if not cards:
            # Tracé quand même : « aucune carte à traiter » est un résultat
            # normal (tout est déjà tagué), et il doit être distinguable d'un run
            # qui n'a jamais eu lieu.
            log.info("Aucune carte à traiter.")
            run_id = ouvrir_run(session, SOURCE)
            finaliser_run(SessionLocal, run_id, "success")
            return

        mode = "toutes les cartes" if args.process_all else "cartes sans tags"
        log.info("%d cartes à traiter (%s).", len(cards), mode)

        run_id = ouvrir_run(session, SOURCE, source_file=GRAPHQL_URL)
        log.info("Import run #%d démarré.", run_id)

        with httpx.Client(cookies={}) as client:
            try:
                csrf_token = refresh_session(client)
            except Exception as exc:
                finaliser_run(SessionLocal, run_id, "failed",
                              error_message=f"session Tagger impossible : {exc}"[:2000])
                log.error("Session Tagger impossible : %s", exc)
                sys.exit(1)
            log.info("Session Tagger initialisée.")

            request_count = 0
            commit_batch: list[tuple[int, list[str]]] = []

            with tqdm(cards, unit="carte", desc="Tags Tagger") as bar:
                for card_id, card_name, set_code, collector_number in bar:
                    bar.set_postfix(tags=total_tags, erreurs=errors)

                    # Rafraîchissement périodique du token CSRF
                    if request_count > 0 and request_count % CSRF_REFRESH_EVERY == 0:
                        try:
                            csrf_token = refresh_session(client)
                        except Exception as e:
                            log.warning("Échec rafraîchissement CSRF : %s", e)

                    try:
                        tag_names = graphql_request(client, csrf_token, set_code, collector_number)
                    except _CsrfExpired:
                        log.info("Token CSRF expiré, rafraîchissement...")
                        try:
                            csrf_token = refresh_session(client)
                            tag_names = graphql_request(
                                client, csrf_token, set_code, collector_number)
                        except Exception as e:
                            log.warning("Échec après rafraîchissement CSRF pour %s : %s",
                                        card_name, e)
                            errors += 1
                            continue
                    except _TaggerIndisponible as e:
                        log.warning("Tagger indisponible pour %s : %s", card_name, e)
                        errors += 1
                        continue
                    except Exception as e:
                        log.warning("Erreur pour %s (%s/%s) : %s",
                                    card_name, set_code, collector_number, e)
                        errors += 1
                        continue

                    request_count += 1
                    cartes_traitees += 1

                    if tag_names is not None:
                        # La carte est enregistrée même sans tag : c'est une
                        # réponse de Tagger, et c'est ce qui évite de la
                        # réinterroger à chaque run.
                        commit_batch.append((card_id, tag_names))
                        total_tags += len(tag_names)

                    # Commit par lots de 100 cartes
                    if len(commit_batch) >= 100:
                        for cid, tags in commit_batch:
                            upsert_tags(session, cid, tags, replace=args.process_all)
                        marquer_verifiees(session, [cid for cid, _ in commit_batch])
                        session.commit()
                        commit_batch.clear()

                    time.sleep(args.delay)

            # Commit du reste
            if commit_batch:
                for cid, tags in commit_batch:
                    upsert_tags(session, cid, tags, replace=args.process_all)
                marquer_verifiees(session, [cid for cid, _ in commit_batch])
                session.commit()

    tentatives = cartes_traitees + errors
    taux = errors / tentatives if tentatives else 0.0
    log.info("Terminé. %d tags importés, %d erreurs sur %d requête(s) (%.1f %%).",
             total_tags, errors, tentatives, taux * 100)

    en_echec = taux > SEUIL_ECHEC
    finaliser_run(
        SessionLocal, run_id,
        "failed" if en_echec else ("partial" if errors else "success"),
        cards=cartes_traitees,
        printings=total_tags,
        errors=errors,
        error_message=(
            f"{errors} echec(s) de transport sur {tentatives} requete(s) "
            f"({taux:.1%}) — Tagger indisponible ?" if errors else None),
    )

    if en_echec:
        log.error(
            "Plus de %.0f %% des requetes ont echoue : Tagger est probablement "
            "indisponible ou son API a change. Run marque 'failed'.", SEUIL_ECHEC * 100)
        sys.exit(1)


if __name__ == "__main__":
    main()
