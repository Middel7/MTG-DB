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

Le script est conçu pour tourner aussi bien à la main qu'en tâche planifiée ou
en conteneur :

- **verrou anti-chevauchement** — `pg_advisory_lock` sur la base visée : deux runs
  ne peuvent pas s'écraser, même lancés depuis deux machines
- **garde-fou** — en conteneur, une `DATABASE_URL` locale est refusée avant écriture
- **idempotence** — un bulk Scryfall déjà importé n'est ni retéléchargé ni re-parsé
- **purge automatique** des anciens bulks (393 Mo pièce), avant téléchargement du suivant
- **journalisation** dans `logs/update_<horodatage>.log` (30 derniers conservés),
  ou sur **stdout** en conteneur — le disque y est éphémère
- **codes de sortie** : `0` succès · `1` échec · `2` run déjà en cours

> Le lanceur `update.ps1` active le venv et l'encodage UTF-8, puis délègue à
> `scripts/update_all.py`. Sous un shell non-Windows, appeler directement :
> `python scripts/update_all.py`.

---

## Mise à jour automatique

Scryfall republie son bulk **environ toutes les 12 h** : deux runs par jour sont
donc pertinents sur la base locale. L'étape *tags* (~40 min) est traitée à part,
une fois par semaine.

**En production** — deux **Cron Jobs Render** construisent le `Dockerfile` de ce
dépôt et l'exécutent contre la base Render. Plus aucun poste n'est sur le chemin
de la production. Tout est déclaré dans [`render.yaml`](render.yaml) : mise en
service par *Dashboard Render → New → Blueprint*.

| Tâche | Où | Fréquence | Durée mesurée |
|---|---|---|---|
| Scryfall + Cardmarket + Game Changers | Render | 01:00 UTC, tous les jours | 90 à 120 min |
| Tags Tagger | Render | 05:00 UTC, le dimanche | longue |
| Scryfall + Cardmarket + Game Changers | poste, base locale | 08:00 et 20:00 | ~8 min 30 |
| Tags Tagger | poste, base locale | dimanche 05:00 | ~40 min |

**En local (Windows)** :

```powershell
.\scripts\install_scheduled_tasks.ps1          # installer les tâches locales
.\scripts\install_scheduled_tasks.ps1 -Remove  # désinstaller
```

**Secours manuel vers la production**, en cas d'incident Render :

```powershell
.\update-prod.ps1 --skip tags
```

> Un run de production dure ~2 h contre 8 min en local. Le goulot n'est ni le
> réseau ni le disque du job, mais l'instance PostgreSQL elle-même (256 Mo de
> RAM, 0,1 vCPU pour une base de 3,4 Go). Mesures détaillées dans
> [`docs/deploiement.md`](docs/deploiement.md).

Détails, coûts, supervision : [`docs/deploiement.md`](docs/deploiement.md).

---

## Les 4 sources de données

| # | Source | Script | Contenu |
|---|---|---|---|
| 1 | **Scryfall** | `import_scryfall.py` | Cartes, éditions, impressions (toutes langues), prix, `cardmarket_id`, `tcgplayer_id`, `printed_name`. Source : bulk `all_cards` JSONL gzippé (393 Mo). |
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

## Tests

```powershell
uv sync --group dev
.venv\Scripts\python.exe -m pytest              # toute la suite
.venv\Scripts\python.exe -m pytest -m "not integration"   # sans base de données
```

Les tests marqués `integration` ont besoin d'une vraie base PostgreSQL (celle de
`DATABASE_URL`) : un verrou consultatif ne se simule pas utilement, c'est le
serveur qui l'arbitre. Ils se sautent d'eux-mêmes si `DATABASE_URL` est absent,
et n'écrivent rien — ils posent un verrou, vérifient `pg_locks`, et relâchent.

---

## Structure du projet

```
MTG-DB/
├─ update.ps1                  # Lanceur de la mise à jour complète (base locale)
├─ update-prod.ps1             # Secours manuel vers la production (plus planifié)
├─ render.yaml                 # ★ Blueprint : les 2 Cron Jobs Render de production
├─ Dockerfile                  # Image "updater" (one-shot) — celle que Render exécute
├─ docker-compose.yml          # Service updater, usage local
├─ CHANGELOG.md
├─ scripts/
│  ├─ update_all.py            # ★ Orchestrateur (les 4 sources)
│  ├─ install_scheduled_tasks.ps1  # Tâches planifiées Windows (base locale)
│  ├─ import_scryfall.py       # Import Scryfall
│  ├─ import_cardmarket_all.py # Import Cardmarket complet
│  ├─ import_cardmarket_products.py
│  ├─ import_cardmarket_price_guide.py
│  ├─ link_cardmarket_to_scryfall.py
│  ├─ import_game_changers.py  # Flag game_changer
│  └─ import_tagger_tags.py    # Tags oracle Scryfall Tagger
├─ src/mtgdb/
│  ├─ runtime.py               # Détection conteneur (journal, garde-fous)
│  ├─ db/
│  │  ├─ engine.py             # Connexion + garde-fou "pas de base locale"
│  │  ├─ urls.py               # Normalisation postgres:// → postgresql://
│  │  ├─ lock.py               # Verrou anti-chevauchement (pg_advisory_lock)
│  │  └─ models/               # modèles SQLAlchemy
│  └─ cardmarket/              # téléchargement + parsers + import Cardmarket
├─ tests/                      # pytest ; les tests `integration` demandent une base
├─ alembic/                    # migrations
├─ docs/
│  ├─ schema_base_de_donnees.txt  # Schéma détaillé (tables, champs, relations)
│  ├─ deploiement.md              # Render, Docker, mesures, supervision
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
