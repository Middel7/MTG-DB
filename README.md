# MTG-DB

Base de données PostgreSQL centralisant les données **Magic: The Gathering** :
cartes, éditions, impressions multilingues, prix (Scryfall + Cardmarket), tags
mécaniques et statistiques de decks Commander.

Le projet fournit les **modèles SQLAlchemy**, les **migrations Alembic** et les
**scripts d'import** qui alimentent et rafraîchissent la base.

---

## Mise à jour de la base

Une seule commande met à jour **toutes** les sources dans le bon ordre
(Scryfall → Cardmarket → Game Changers → Tags Tagger) :

```powershell
.\update.ps1
```

Options courantes :

| Commande | Effet |
|---|---|
| `.\update.ps1 --dry-run` | Affiche le plan sans rien exécuter |
| `.\update.ps1 --only cardmarket` | Ne lance qu'une source |
| `.\update.ps1 --skip tags` | Tout sauf les tags Tagger |
| `.\update.ps1 --force` | Réimporte le bulk Scryfall même s'il l'a déjà été |
| `.\update.ps1 --tags-all` | Retraite **tous** les tags (long, remplace l'existant) |
| `.\update.ps1 --stop-on-error` | Arrête à la première étape en échec |

Étapes disponibles pour `--only` / `--skip` :
`scryfall` · `cardmarket` · `game-changers` · `tags`

Chaque étape s'exécute dans un sous-processus isolé : une étape qui échoue
n'interrompt pas les suivantes (sauf `--stop-on-error`). Un **rapport final**
récapitule le statut et la durée de chaque étape.

Le script est conçu pour tourner aussi bien à la main qu'en tâche planifiée :

- **verrou anti-chevauchement** — deux runs ne peuvent pas s'écraser mutuellement
- **idempotence** — un bulk Scryfall déjà importé n'est ni retéléchargé ni re-parsé
- **purge automatique** des anciens bulks (2,4 Go pièce)
- **journalisation** dans `logs/update_<horodatage>.log` (30 derniers conservés)
- **codes de sortie** : `0` succès · `1` échec · `2` run déjà en cours

> Le lanceur `update.ps1` active le venv et l'encodage UTF-8, puis délègue à
> `scripts/update_all.py`. Sous un shell non-Windows, appeler directement :
> `python scripts/update_all.py`.

---

## Mise à jour automatique

Scryfall republie son bulk **environ toutes les 12 h** : deux runs par jour sont
donc pertinents. L'étape *tags* (~40 min) est traitée à part, une fois par semaine.

| Tâche | Fréquence | Durée |
|---|---|---|
| Scryfall + Cardmarket + Game Changers | 2×/jour (08:00, 20:00) | ~7 min, ou quelques secondes s'il n'y a rien de neuf |
| Tags Tagger | 1×/semaine (dimanche 03:00) | ~40 min |

**En local (Windows)** :

```powershell
.\scripts\install_scheduled_tasks.ps1          # installer les tâches planifiées
.\scripts\install_scheduled_tasks.ps1 -Remove  # désinstaller
```

**En production (Docker)** — conteneur one-shot déclenché par un ordonnanceur :

```bash
docker compose run --rm updater --skip tags
```

Détails, cron, supervision : [`docs/deploiement.md`](docs/deploiement.md).

---

## Les 4 sources de données

| # | Source | Script | Contenu |
|---|---|---|---|
| 1 | **Scryfall** | `import_scryfall.py` | Cartes, éditions, impressions (toutes langues), prix, `cardmarket_id`, `tcgplayer_id`, `printed_name`. Source : bulk `all_cards` (~2,4 Go). |
| 2 | **Cardmarket** | `import_cardmarket_all.py` | Product Catalog + Price Guide (prix foil/non-foil) + rapport de liaison Scryfall ↔ Cardmarket. |
| 3 | **Game Changers** | `import_game_changers.py` | Flag `game_changer` sur les cartes (`is:gamechanger` Scryfall). |
| 4 | **Tags Tagger** | `import_tagger_tags.py` | `ORACLE_CARD_TAG` depuis Scryfall Tagger (API GraphQL non officielle). Par défaut : cartes sans tags uniquement. |

Détail de chaque script : voir [`docs/schema_base_de_donnees.txt`](docs/schema_base_de_donnees.txt).

---

## Installation

Prérequis : **Python ≥ 3.12**, **PostgreSQL**, et [`uv`](https://docs.astral.sh/uv/)
(ou `venv` + `pip`).

```powershell
# 1. Environnement virtuel + dépendances
uv venv
uv sync

# 2. Configuration de la connexion
Copy-Item .env.example .env
#   puis éditer .env : DATABASE_URL=postgresql://user:pass@host:port/dbname

# 3. Créer le schéma (migrations Alembic)
alembic upgrade head

# 4. Première mise à jour complète
.\update.ps1
```

La connexion est lue depuis `DATABASE_URL` (fichier `.env` à la racine, jamais
commité). Voir `src/mtgdb/db/engine.py`.

---

## Structure du projet

```
MTG-DB/
├─ update.ps1                  # Lanceur de la mise à jour complète
├─ Dockerfile                  # Image "updater" (one-shot) pour la production
├─ docker-compose.yml          # Service updater
├─ scripts/
│  ├─ update_all.py            # ★ Orchestrateur (les 4 sources)
│  ├─ install_scheduled_tasks.ps1  # Tâches planifiées Windows
│  ├─ import_scryfall.py       # Import Scryfall
│  ├─ import_cardmarket_all.py # Import Cardmarket complet
│  ├─ import_cardmarket_products.py
│  ├─ import_cardmarket_price_guide.py
│  ├─ link_cardmarket_to_scryfall.py
│  ├─ import_game_changers.py  # Flag game_changer
│  └─ import_tagger_tags.py    # Tags oracle Scryfall Tagger
├─ src/mtgdb/
│  ├─ db/                      # engine, base, modèles SQLAlchemy
│  └─ cardmarket/              # téléchargement + parsers + import Cardmarket
├─ alembic/                    # migrations
├─ docs/
│  ├─ schema_base_de_donnees.txt  # Schéma détaillé (tables, champs, relations)
│  ├─ deploiement.md              # Planification, Docker, supervision
│  ├─ migrations.md               # Base partagée, garde-fous Alembic
│  ├─ recherche_trigram.md        # Index GIN trigram sur printed_name
│  └─ Launch.txt                  # Aide-mémoire des commandes
├─ logs/                       # journaux des runs (git-ignorés)
└─ data/raw/                   # fichiers bruts téléchargés (git-ignorés)
```

---

## Schéma de la base

Principales tables (préfixe `scryfall_` ou `cardmarket_`) :

- **`scryfall_cards`** — carte oracle (nom, coût, type, texte, couleurs, mots-clés…)
- **`scryfall_card_printings`** — chaque impression (édition, langue, rareté,
  images, `cardmarket_id`, `tcgplayer_id`, `tcgplayer_id_en`…)
- **`scryfall_card_faces`** — faces des cartes double-face / recto-verso
- **`scryfall_card_prices`** — historique de prix Scryfall (append-only, 1 ligne/jour)
- **`scryfall_card_tags`** — tags mécaniques (Tagger)
- **`scryfall_mtg_sets`** — éditions
- **`cardmarket_products`** — catalogue produits Cardmarket
- **`cardmarket_price_guide_entries`** — prix Cardmarket foil/non-foil historisés
- **`deck_stat_global` / `deck_stat_commander`** — statistiques de decks Commander
  (alimentées par un script externe, hors périmètre de `update_all.py`)

Schéma complet, relations et notes détaillées :
[`docs/schema_base_de_donnees.txt`](docs/schema_base_de_donnees.txt).

---

## Migrations (Alembic)

```powershell
alembic current                               # révision courante
alembic upgrade head                          # appliquer les migrations
alembic revision --autogenerate -m "message"  # générer une migration
alembic downgrade -1                          # revenir en arrière d'une révision
```

> ⚠️ **La base `manamind` est partagée avec d'autres projets** (ManaMind_AI, mtgtrade),
> qui possèdent leurs propres tables (`users`, `deck_cards`, `deck_stat_*`…) et leur
> propre historique de migrations. MTG-DB utilise sa table de version dédiée
> `mtgdb_alembic_version`, et `alembic/env.py` masque à l'autogenerate les tables des
> autres projets — **sans ces garde-fous, un autogenerate détruirait la moitié de la
> base**. Relisez toujours la migration générée avant de l'appliquer.
>
> Détails et dette connue : [`docs/migrations.md`](docs/migrations.md).
