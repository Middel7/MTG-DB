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
  python scripts/import_tagger_tags.py --all            # toutes les cartes (remplace les tags existants)
  python scripts/import_tagger_tags.py --limit 500      # limite à N cartes (test)
  python scripts/import_tagger_tags.py --delay 0.3      # délai entre requêtes (défaut : 0.2s)

Notes :
  - L'API GraphQL du Tagger est non officielle et peut changer sans préavis.
  - En cas d'erreur HTTP, le script continue et log les échecs.
  - Le token CSRF est rafraîchi toutes les 200 requêtes ou sur erreur d'authentification.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.db.engine import SessionLocal, check_connection
from mtgdb.db.models.card import Card
from mtgdb.db.models.card_tag import CardTag
from mtgdb.db.models.card_printing import CardPrinting

TAGGER_BASE = "https://tagger.scryfall.com"
GRAPHQL_URL = f"{TAGGER_BASE}/graphql"
CSRF_REFRESH_EVERY = 200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
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
    Appelle l'endpoint GraphQL du Tagger et retourne la liste des ORACLE_CARD_TAG.
    Retourne None si la carte n'est pas trouvée ou en cas d'erreur.
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
        "Referer": f"{TAGGER_BASE}/card/{set_code}/{collector_number}",
        "Origin": TAGGER_BASE,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    for attempt in range(3):
        try:
            resp = client.post(GRAPHQL_URL, json=payload, headers=headers, timeout=15)
        except httpx.TimeoutException:
            log.warning("Timeout pour %s/%s", set_code, collector_number)
            return None

        if resp.status_code == 429:
            wait = [10, 15, 20][attempt]  # 10s, 15s, 20s
            log.warning("429 Too Many Requests — attente %ds avant retry %d/3", wait, attempt + 1)
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            log.warning("HTTP %s pour %s/%s", resp.status_code, set_code, collector_number)
            return None

        break
    else:
        log.error("Abandon après 3 tentatives (429) pour %s/%s", set_code, collector_number)
        return None

    body = resp.json()

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
    pass


# ══════════════════════════════════════════════════════════════════════════════
# SÉLECTION DES CARTES À TRAITER
# ══════════════════════════════════════════════════════════════════════════════

def fetch_cards_to_process(session: Session, only_missing: bool, limit: Optional[int]) -> list[tuple[int, str, str, str]]:
    """
    Retourne une liste de (card_id, card_name, set_code, collector_number).
    Choisit une impression anglaise en priorité, sinon la première disponible.
    Si only_missing=True, exclut les cartes qui ont déjà au moins un tag.
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
        """ if only_missing else "",
        limit_clause=f"LIMIT {limit}" if limit else "",
    ))
    rows = session.execute(sql).fetchall()
    return [(r.card_id, r.card_name, r.set_code, r.collector_number) for r in rows]


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

    with SessionLocal() as session:
        log.info("Récupération des cartes à traiter...")
        cards = fetch_cards_to_process(session, only_missing=only_missing, limit=args.limit)

        if not cards:
            log.info("Aucune carte à traiter.")
            return

        mode = "toutes les cartes" if args.process_all else "cartes sans tags"
        log.info("%d cartes à traiter (%s).", len(cards), mode)

        with httpx.Client(cookies={}) as client:
            csrf_token = refresh_session(client)
            log.info("Session Tagger initialisée.")

            total_tags = 0
            errors = 0
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
                            tag_names = graphql_request(client, csrf_token, set_code, collector_number)
                        except Exception as e:
                            log.warning("Échec après rafraîchissement CSRF pour %s : %s", card_name, e)
                            errors += 1
                            continue
                    except Exception as e:
                        log.warning("Erreur pour %s (%s/%s) : %s", card_name, set_code, collector_number, e)
                        errors += 1
                        continue

                    request_count += 1

                    if tag_names is not None:
                        commit_batch.append((card_id, tag_names))
                        total_tags += len(tag_names)

                    # Commit par lots de 100 cartes
                    if len(commit_batch) >= 100:
                        for cid, tags in commit_batch:
                            upsert_tags(session, cid, tags, replace=args.process_all)
                        session.commit()
                        commit_batch.clear()

                    time.sleep(args.delay)

            # Commit du reste
            if commit_batch:
                for cid, tags in commit_batch:
                    upsert_tags(session, cid, tags, replace=args.process_all)
                session.commit()

    log.info("Terminé. %d tags importés, %d erreurs.", total_tags, errors)


if __name__ == "__main__":
    main()
