#!/usr/bin/env python3
"""
Fraîcheur du catalogue : ce que les sources ont publié, ce que MTG-DB a absorbé.

Répond à trois questions que ni les logs ni `import_runs` ne couvraient
ensemble :

    quand Scryfall a-t-il publié une nouvelle version de son bulk ?
    quand Cardmarket a-t-il republié ses exports ?
    quand MTG-DB a-t-il absorbé tout cela ?

Usage :
  python scripts/fraicheur.py                 # tableau de l'état courant
  python scripts/fraicheur.py --historique    # les dernières publications vues
  python scripts/fraicheur.py --json          # même chose, exploitable par un script
  python scripts/fraicheur.py --check         # code 1 si quelque chose décroche

Le mode `--check` est fait pour être lancé par un Cron Job Render : Render
envoie un e-mail dès qu'un job sort en erreur, ce qui donne l'alerte sans
qu'aucun secret SMTP n'ait à être stocké ni maintenu quelque part.

Codes de sortie :
  0  tout est à jour
  1  au moins un décrochage détecté (uniquement en mode --check)
  2  impossible de lire l'état (base injoignable, vue absente)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.db.engine import SessionLocal  # noqa: E402
from mtgdb.db.publications import historique_publications, lire_fraicheur  # noqa: E402

# Libellés lisibles. Les identifiants techniques restent en base ; c'est
# l'affichage qui s'adapte à l'humain, pas l'inverse.
LIBELLES = {
    "scryfall_bulk": "Scryfall — bulk all_cards",
    "cardmarket_price_guide": "Cardmarket — price guide",
    "cardmarket_product_catalog": "Cardmarket — catalogue produits",
    "tagger": "Scryfall Tagger — tags",
}

# Les sources qu'on s'attend à voir suivies. Une absence n'est pas un détail :
# elle signifie que l'étape correspondante n'a pas tourné une seule fois depuis
# la mise en place du suivi, ce que ni les compteurs ni les durées ne montrent.
SOURCES_ATTENDUES = (
    "scryfall_bulk",
    "cardmarket_price_guide",
    "cardmarket_product_catalog",
    "tagger",
)

# Seuils de décrochage, par défaut. Ils tiennent compte du rythme réel de chaque
# source, mesuré sur septembre 2026 : Scryfall publie 2×/jour, le price guide
# Cardmarket 1×/jour vers 00:42 UTC, le catalogue produits de façon irrégulière.
RETARD_MAX_H = 6      # une version publiée qui attend plus longtemps est un décrochage
VEILLE_MAX_H = 3      # au-delà, c'est le pipeline lui-même qui ne passe plus
TAGS_MAX_JOURS = 9    # le job de tags est hebdomadaire : 9 jours laissent un run de marge


def humaniser(duree: timedelta | None) -> str:
    """Rend une durée lisible d'un coup d'œil, sans microsecondes ni jargon."""
    if duree is None:
        return "—"
    secondes = int(duree.total_seconds())
    if secondes < 0:
        return "à l'instant"
    jours, reste = divmod(secondes, 86400)
    heures, reste = divmod(reste, 3600)
    minutes = reste // 60
    if jours:
        return f"{jours} j {heures:02d} h"
    if heures:
        return f"{heures} h {minutes:02d}"
    return f"{minutes} min"


def horodatage(valeur: datetime | None) -> str:
    return valeur.strftime("%d/%m %H:%M") if valeur else "—"


def afficher_tableau(lignes: list[dict]) -> None:
    entetes = ("Source", "Vérifiée", "Publiée", "Absorbée", "Retard", "État")
    largeurs = (33, 13, 13, 13, 12, 10)

    print()
    print("  " + "".join(t.ljust(w) for t, w in zip(entetes, largeurs, strict=True)))
    print("  " + "─" * sum(largeurs))

    for ligne in lignes:
        a_jour = ligne["a_jour"]
        etat = "à jour" if a_jour else "EN ATTENTE"
        cellules = (
            LIBELLES.get(ligne["source"], ligne["source"]),
            humaniser(ligne["depuis_derniere_verification"]),
            horodatage(ligne["publiee_le"]),
            horodatage(ligne["dernier_import_le"]),
            humaniser(ligne["retard"]),
            etat,
        )
        print("  " + "".join(str(c).ljust(w) for c, w in zip(cellules, largeurs, strict=True)))

    print()
    print("  Vérifiée : il y a combien de temps le pipeline a interrogé la source.")
    print("  Publiée  : date annoncée par la source pour sa dernière version.")
    print("  Absorbée : quand MTG-DB a intégré cette version.")
    print()


def afficher_historique(lignes: list[dict]) -> None:
    print()
    print("  Publication                                  Publiée le     Absorbée après")
    print("  " + "─" * 76)
    for ligne in lignes:
        version = str(ligne["version"])
        if len(version) > 42:
            version = version[:39] + "…"
        source = LIBELLES.get(ligne["source"], ligne["source"]).split(" — ")[0]
        delai = humaniser(ligne["delai_absorption"]) if ligne["imported_at"] else "pas encore"
        print(f"  {source:<12} {version:<30} "
              f"{horodatage(ligne['published_at']):<14} {delai}")
    print()


def detecter_decrochages(lignes: list[dict], retard_max_h: int,
                         veille_max_h: int, tags_max_jours: int) -> list[str]:
    """
    Retourne la liste des anomalies, vide si tout va bien.

    Trois motifs, et trois seulement — chacun correspond à une panne distincte
    qu'on ne verrait pas autrement :

      retard     la source a publié, le pipeline tourne, mais l'import échoue ;
      veille     le pipeline ne passe plus du tout (cron arrêté, build cassé) ;
      tags       le job hebdomadaire n'a pas tourné depuis plus d'une semaine.
    """
    # Les écarts sont calculés côté SQL, sur l'horloge du serveur : c'est la
    # seule qui fasse foi quand le job tourne ailleurs que sur ce poste.
    anomalies: list[str] = []

    for ligne in lignes:
        source = LIBELLES.get(ligne["source"], ligne["source"])

        if ligne["source"] == "tagger":
            depuis = ligne["depuis_dernier_import"]
            if depuis is None or depuis > timedelta(days=tags_max_jours):
                anomalies.append(
                    f"{source} : aucun import réussi depuis {humaniser(depuis)} "
                    f"(seuil {tags_max_jours} j)")
            continue

        retard = ligne["retard"]
        if retard is not None and retard > timedelta(hours=retard_max_h):
            anomalies.append(
                f"{source} : version « {ligne['derniere_version']} » publiée il y a "
                f"{humaniser(retard)} et toujours pas absorbée (seuil {retard_max_h} h)")

        veille = ligne["depuis_derniere_verification"]
        if veille is None or veille > timedelta(hours=veille_max_h):
            anomalies.append(
                f"{source} : plus interrogée depuis {humaniser(veille)} — le pipeline "
                f"ne tourne probablement plus (seuil {veille_max_h} h)")

    connues = {ligne["source"] for ligne in lignes}
    for attendue in SOURCES_ATTENDUES:
        if attendue not in connues:
            anomalies.append(
                f"{LIBELLES.get(attendue, attendue)} : jamais suivie. L'étape n'a pas "
                f"tourné depuis la mise en place du suivi, ou elle échoue avant "
                f"d'interroger la source.")

    return anomalies


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fraîcheur des sources et du catalogue MTG-DB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true",
                        help="Sort en code 1 si un décrochage est détecté (pour un cron).")
    parser.add_argument("--historique", nargs="?", type=int, const=20, metavar="N",
                        help="Affiche les N dernières publications (défaut : 20).")
    parser.add_argument("--json", action="store_true",
                        help="Sortie JSON plutôt que tableau.")
    parser.add_argument("--retard-max", type=int, default=RETARD_MAX_H, metavar="H",
                        help=f"Heures avant qu'une version non absorbée alerte "
                             f"(défaut : {RETARD_MAX_H}).")
    parser.add_argument("--veille-max", type=int, default=VEILLE_MAX_H, metavar="H",
                        help=f"Heures avant qu'une source non interrogée alerte "
                             f"(défaut : {VEILLE_MAX_H}).")
    parser.add_argument("--tags-max", type=int, default=TAGS_MAX_JOURS, metavar="J",
                        help=f"Jours avant que les tags alertent (défaut : {TAGS_MAX_JOURS}).")
    args = parser.parse_args()

    if SessionLocal is None:
        print("DATABASE_URL absent : impossible de lire la fraîcheur.", file=sys.stderr)
        sys.exit(2)

    try:
        with SessionLocal() as session:
            lignes = lire_fraicheur(session)
            historique = (historique_publications(session, args.historique)
                          if args.historique else [])
    except Exception as exc:  # noqa: BLE001
        print(f"Lecture impossible : {exc}", file=sys.stderr)
        sys.exit(2)

    anomalies = detecter_decrochages(lignes, args.retard_max, args.veille_max, args.tags_max)

    if args.json:
        print(json.dumps(
            {"sources": lignes, "historique": historique, "anomalies": anomalies},
            default=str, ensure_ascii=False, indent=2))
    else:
        afficher_tableau(lignes)
        if args.historique:
            afficher_historique(historique)
        if anomalies:
            print("  ⚠ Décrochages détectés :")
            for anomalie in anomalies:
                print(f"    - {anomalie}")
            print()
        elif args.check:
            print("  ✓ Toutes les sources sont à jour.")
            print()

    # Le code 1 n'est rendu qu'en mode --check : une consultation qui échouerait
    # parce que le catalogue a deux heures de retard serait pénible au quotidien,
    # alors que c'est exactement ce qu'on attend d'une sonde.
    if args.check and anomalies:
        sys.exit(1)


if __name__ == "__main__":
    main()
