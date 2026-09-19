# Déploiement & mise à jour automatique

La logique de mise à jour vit **entièrement dans `scripts/update_all.py`**. Les
ordonnanceurs (Cron Job Render, Planificateur Windows, cron, Ofelia, Kubernetes)
ne font que le *déclencher* : changer d'ordonnanceur ou d'hébergement ne demande
aucune réécriture.

Depuis le **19/09/2026**, la base de production est alimentée par **deux Cron
Jobs Render** décrits dans [`render.yaml`](../render.yaml). Aucune tâche
planifiée Windows ne vise plus la production.

---

## Vue d'ensemble

| Cible | Qui déclenche | Quand | Commande |
|---|---|---|---|
| **Production** (Render `relictrade`) | Cron Job Render `mtgdb-catalogue-quotidien` | 01:00 UTC, tous les jours | `python scripts/update_all.py --skip tags` |
| **Production** | Cron Job Render `mtgdb-tags-hebdomadaire` | 05:00 UTC, le dimanche | `python scripts/update_all.py --only tags` |
| **Locale** (`manamind`) | Planificateur Windows `MTG-DB Update` | 08:00 et 20:00 | `update.ps1 --skip tags` |
| **Locale** | Planificateur Windows `MTG-DB Tags` | dimanche 05:00 | `update.ps1 --only tags` |
| **Production**, secours manuel | vous | en cas d'incident Render | `.\update-prod.ps1 --skip tags` |

Render interprète les expressions cron **en UTC** : 01:00 UTC = 02:00 à Paris
l'hiver, 03:00 l'été.

---

## Garanties du script

| Garantie | Détail |
|---|---|
| **Verrou anti-chevauchement** | `pg_advisory_lock` sur la **base visée**. Un second run sort en **code 2** sans rien faire, y compris s'il vient d'une autre machine. Aucun verrou périmé à nettoyer : PostgreSQL le libère dès que la session tombe. |
| **Garde-fou base locale** | En conteneur (ou avec `MTGDB_REQUIRE_REMOTE_DB=1`), une `DATABASE_URL` qui pointe sur `localhost` fait échouer le run **avant la première écriture**. |
| **Normalisation d'URL** | `postgres://` → `postgresql://` sur les trois points de lecture : `mtgdb.db.engine`, `alembic/env.py`, `scripts/import_game_changers.py`. |
| **Idempotence** | Un bulk Scryfall déjà importé n'est **ni retéléchargé ni re-parsé** (comparaison de `import_runs.source_file`, en base — pas sur le disque). |
| **Purge automatique** | Les anciens bulks (393 Mo pièce) sont supprimés **avant** le téléchargement du suivant : le pic disque est d'un seul fichier. |
| **Journalisation** | Sur un poste : `logs/update_<horodatage>.log`, 30 derniers conservés. En conteneur : **stdout uniquement**, le disque étant éphémère. |
| **Reprise sur coupure** | Une interruption transitoire de la base (recovery, SSL coupé, connexion refusée) déclenche jusqu'à 5 réessais espacés de 5 s à 2 min, avec recyclage du pool. Les erreurs de données, elles, ne sont jamais rejouées. |
| **Statut honnête** | Un run qui a perdu des cartes est marqué `partial`, pas `success` : le bulk sera repris au run suivant et la supervision voit le décrochage. |
| **Runs orphelins** | Un run resté `running` depuis plus de 6 h est marqué `failed` par le run suivant. |
| **Codes de sortie** | `0` succès · `1` au moins une étape en échec · `2` un run est déjà en cours. |

---

## Production : les Cron Jobs Render

### Mise en service

Dashboard Render → **New** → **Blueprint** → dépôt `Middel7/MTG-DB`. Render lit
`render.yaml` et propose les deux services. Il demandera la valeur de
`DATABASE_URL`, marquée `sync: false` parce qu'elle n'a rien à faire dans le
dépôt.

Coller l'**URL interne** de la base (dashboard de la base → *Internal Database
URL*), pas l'externe :

- elle ne sort pas du réseau Render — ni bande passante sortante, ni traversée
  d'Internet ;
- elle évite la coupure SSL qui a fait échouer le run du 17/09 après 78 minutes
  de travail (`SSL connection has been closed unexpectedly`).

⚠️ Les deux jobs doivent être dans la **région `frankfurt`**, celle de la base :
l'URL interne n'est joignable que depuis la même région. Un job créé dans la
région par défaut (Oregon) ne verrait tout simplement pas la base. `render.yaml`
le fixe déjà.

### Ce que Render garantit

| Point | Réalité |
|---|---|
| Disque | **Éphémère uniquement** — un cron job ne peut pas recevoir de disque persistant. Sans conséquence : le bulk se retélécharge en 3 s (393 Mo à 108 Mo/s mesurés) et l'idempotence repose sur la base, pas sur le disque. |
| Timeout | Un run est tué après **12 h**. Un run mesuré dure ~2 h. |
| Chevauchement | Render **retarde** le run suivant tant que le précédent tourne (garantie d'exécution unique) — mais **par service** : rien ne coordonne les deux jobs entre eux, d'où le verrou côté base. |
| Facturation | À la seconde, selon le plan de compute, **minimum 1 $/mois** par cron job. |

### Coût

Un run quotidien de ~105 min représente ~53 h/mois. Sur le plan `standard`
(1 vCPU / 2 Go), cela fait **environ 1,85 $/mois**, auxquels s'ajoute le job
hebdomadaire, facturé au minimum de 1 $. **Ordre de grandeur : 3 $/mois.**

Le plan `standard` est dimensionné sur le **pic mémoire mesuré du pipeline,
350 Mo** (67 Mo pour le parse streaming Scryfall, 203 Mo pour le `json.load`
des exports Cardmarket). Le plan `starter` (512 Mo) tiendrait, mais sans marge.

---

## Pourquoi un run de production dure 2 h

Un run complet prend **90 à 120 min** contre **8 min 30** sur la base locale.
Décomposition du run du 19/09 :

| Sous-étape | Prod | Local |
|---|---|---|
| Téléchargement du bulk (393 Mo) | 3 s | 3 s |
| Upsert de 520 463 impressions (1 041 batches) | 84 min | 6 min 30 |
| `UPDATE` propagation `cardmarket_id` — **une seule requête SQL** | **24 min** | 68 s |
| `UPDATE` propagation `tcgplayer_id_en` — une seule requête | 2 min 37 | 8 s |

Les deux `UPDATE` de propagation s'exécutent **intégralement côté serveur**, en
un seul aller-retour. Ils sont 20× plus lents en production. **Aucun réseau
n'explique cela** : le goulot est l'instance PostgreSQL elle-même.

Mesures du lien poste → base, pour écarter définitivement l'hypothèse réseau :
RTT SQL médian **22 ms**, débit d'upload applicatif **20-25 Mbit/s**. Le run
fait ~7 000 aller-retours, soit ~2,6 min de latence cumulée et ~6 min de
transfert : **~9 min sur 110**, la seule part que le passage en interne
supprime.

La cause, lue directement sur la base de production :

```
shared_buffers       = 64 MB      effective_cache_size = 192 MB
max_parallel_workers = 1          max_connections      = 103
taille de la base    = 3432 MB
```

C'est la signature du plus petit plan payant (**Basic-256mb : 256 Mo de RAM,
0,1 vCPU**) pour une base de 3,4 Go : le cache couvre 2 % des données, et
`pg_stat_activity` montre les `INSERT` en attente sur `IO/DataFileRead`.

> **Le passage au cloud supprime la dépendance au poste, pas la lenteur.**
> Le seul levier sur la durée est le plan de la base — décision qui engage aussi
> RELIC-Trade, qui partage cette instance.

---

## Résistance aux coupures de base

Deux runs ont été perdus en trois jours, pour la même raison de fond :

| Date | Origine | Erreur | Perdu |
|---|---|---|---|
| 17/09 | poste Windows | `SSL connection has been closed unexpectedly` | 78 min |
| 19/09 | Cron Job Render | `the database system is not yet accepting connections` / `Consistent recovery state has not been yet reached` | 70 min |

Le second est instructif : l'enchaînement exact était un batch en échec → le
`session.rollback()` du bloc de rattrapage tente de rouvrir une connexion et
lève **à son tour** → l'exception remonte hors de tout `except` → le
`session.commit()` final échoue pour la même raison → le processus meurt sans
jamais pouvoir marquer le run `failed`, qui reste `running` pour toujours.

Trois mécanismes répondent à cela, dans `mtgdb.db.retry` et
`scripts/import_scryfall.py` :

1. **Réessai** — un batch ou une propagation qui échoue sur une erreur de
   transport est rejoué jusqu'à 5 fois (5 s, 15 s, 30 s, 1 min, 2 min), avec
   `engine.dispose()` entre chaque pour ne pas réutiliser un socket mort. Les
   opérations concernées sont toutes idempotentes (`ON CONFLICT DO UPDATE`,
   `UPDATE` conditionnels). Une erreur de contrainte ou de syntaxe n'est
   **jamais** rejouée : elle ne guérirait pas en attendant.
2. **Nettoyage jamais fatal** — `_safe_rollback()` ne lève pas, et la
   finalisation du run passe par une **session neuve** plutôt que par la session
   accidentée, dont un `rollback` expirerait les objets ORM et perdrait
   silencieusement le statut qu'on vient d'y écrire.
3. **Runs orphelins** — au démarrage, tout run `running` de plus de 6 h est
   marqué `failed`. Le seuil n'est qu'une sécurité : c'est le verrou advisory
   qui garantit qu'aucun autre run ne tourne vraiment.

Enfin, la connexion qui porte le verrou est en **`AUTOCOMMIT`**. Sans cela,
SQLAlchemy ouvre une transaction implicite au premier battement du heartbeat et
ne la referme jamais : la session reste `idle in transaction` pendant les deux
heures du run, ce qui gèle l'horizon de `VACUUM` et empêche de recycler les
tuples morts des 520 000 lignes réécrites — aggravant exactement l'IO qu'on
cherche à réduire.

---

## Poste local

```powershell
.\scripts\install_scheduled_tasks.ps1          # installer les tâches locales
.\scripts\install_scheduled_tasks.ps1 -Remove  # désinstaller (y compris les anciennes tâches prod)
```

Aucun droit administrateur requis. Les tâches tournent sous l'utilisateur
courant, lorsqu'il est connecté ; `-StartWhenAvailable` rattrape les exécutions
manquées si la machine était éteinte.

```powershell
Get-ScheduledTask -TaskName 'MTG-DB*'                    # vérifier
Start-ScheduledTask -TaskName 'MTG-DB Update'            # lancer maintenant
Get-ScheduledTaskInfo -TaskName 'MTG-DB Update'          # dernier résultat
```

`LastTaskResult` vaut `0` en cas de succès, `1` si une étape a échoué, `2` si un
run était déjà en cours.

Les créneaux locaux n'ont plus besoin d'être espacés de ceux de la production :
le verrou est désormais posé sur la **base visée**, et un run vers `manamind` ne
bloque plus un run vers `relictrade`.

---

## Secours manuel vers la production

`update-prod.ps1` reste dans le dépôt pour deux cas : un incident Render, ou un
rattrapage qu'on ne veut pas attendre jusqu'au créneau suivant.

```powershell
.\update-prod.ps1 --skip tags
```

Il charge `.env.prod` (git-ignoré), exporte ses variables, pose
`MTGDB_REQUIRE_REMOTE_DB=1` pour armer le garde-fou hors conteneur, puis appelle
`update.ps1`. Cela fonctionne parce que les trois points de chargement du `.env`
appellent `load_dotenv()` **sans `override`** : une variable déjà présente dans
l'environnement l'emporte, et le `.env` de développement n'est jamais modifié.

Les deux garde-fous que ce script portait autrefois vivent maintenant en Python,
donc sur **tous** les chemins d'exécution :

| Garde-fou | Où il vit désormais |
|---|---|
| `postgres://` → `postgresql://` | `mtgdb.db.urls.normalize_database_url()` |
| Refus d'une base locale | `mtgdb.db.engine.assert_remote_database()` |

---

## Docker en local

Le conteneur est **one-shot** : il fait le job puis s'arrête. Il n'y a
volontairement **pas de cron à l'intérieur** — un conteneur qui tourne 24/7 pour
ne rien faire 99 % du temps est un anti-pattern, et cron n'hérite pas des
variables d'environnement Docker (piège classique : `DATABASE_URL` introuvable
au déclenchement).

```bash
export DATABASE_URL=postgresql://user:pass@postgres:5432/manamind

docker compose build updater
docker compose run --rm updater python scripts/update_all.py --dry-run  # plan
docker compose run --rm updater                                          # --skip tags (le CMD)
docker compose run --rm updater python scripts/update_all.py --only tags
```

L'image déclare un **`CMD`** et non un `ENTRYPOINT` : Render remplace le `CMD`
par le champ `dockerCommand` du service, mais jamais l'`ENTRYPOINT`. Avec un
`ENTRYPOINT`, `render.yaml` aurait porté un `dockerCommand: --skip tags`
illisible, dont le sens dépend d'une ligne du Dockerfile qu'on n'a pas sous les
yeux. Conséquence locale : toute variante s'écrit en **commande complète**, pas
en arguments.

En conteneur, le journal part sur **stdout** — pas de `logs/`, le disque étant
éphémère. `MTGDB_CONTAINER=0` au lancement rétablit le comportement « poste de
travail » pour reproduire un incident.

Le garde-fou refuse une base locale vue depuis un conteneur. Pour viser une base
publiée sur l'hôte, le dire explicitement :

```bash
MTGDB_ALLOW_LOCAL_DB=1 docker compose run --rm updater
```

---

## Variables d'environnement

| Variable | Rôle |
|---|---|
| `DATABASE_URL` | Connexion. Normalisée automatiquement si elle commence par `postgres://`. |
| `SKIP_SCRYFALL_PRICES` | `1` en production : n'écrit plus `scryfall_card_prices`. |
| `MTGDB_CONTAINER` | Posé à `1` par le Dockerfile. Journal sur stdout + garde-fou actif. `0` force le mode poste. |
| `MTGDB_REQUIRE_REMOTE_DB` | Arme le garde-fou hors conteneur. Posé par `update-prod.ps1`. |
| `MTGDB_ALLOW_LOCAL_DB` | Lève le garde-fou. À n'utiliser qu'en connaissance de cause. |
| `TZ` | Fuseau d'affichage des horodatages du journal. |

### `SKIP_SCRYFALL_PRICES` en production

Motif vérifié dans le dépôt consommateur : `apps/api/AGENTS.md` de RELIC-Trade
**interdit** l'usage de `scryfall_card_prices` (« Ne jamais utiliser […] pour un
prix dans ce projet »), les prix venant de `cardmarket_price_guide_entries`. Les
écrire coûtait ~344 000 lignes par run pour personne.

⚠️ **Le flag arrête l'écriture, il ne supprime rien.** La table contient
**4 061 065 lignes du 07/06 au 30/08/2026, soit 738 Mo** d'historique **non
reconstituable**. Ne jamais la purger sans décision explicite.

---

## Migrations

Le run **n'applique jamais** les migrations, ni en local ni sur Render. Avant de
déployer un changement de schéma :

```powershell
$env:DATABASE_URL = ((Get-Content .env.prod | Where-Object { $_ -like 'DATABASE_URL=*' }) -replace '^DATABASE_URL=','')
.venv\Scripts\python.exe -m alembic upgrade head
```

`alembic/env.py` normalise désormais lui aussi le préfixe `postgres://` : c'est
le chemin qu'on emprunte à la main, au moment précis où l'on n'a pas envie de
déboguer une URL.

⚠️ `relictrade` héberge **deux** chaînes Alembic : `mtgdb_alembic_version` (ce
dépôt) et `mtgtrade_alembic_version` (RELIC-Trade). Les garde-fous d'`env.py`
(`version_table` + `include_object`) sont indispensables. Contrairement à la
base locale `manamind`, ManaMind_AI n'est **pas** présent en production.

---

## Supervision

Côté RELIC-Trade, `GET /health/catalog` surveille déjà la fraîcheur du catalogue
et renvoie 503 si l'alimentation s'arrête. Rien à construire en plus.

Pour un diagnostic direct :

```sql
SELECT max(finished_at) AS dernier_import_reussi
FROM import_runs
WHERE source = 'scryfall' AND status = 'success';
```

Sur Render, les journaux d'un run sont dans le dashboard du cron job (*Logs*),
et l'onglet *Events* donne l'historique des exécutions et leur statut.

Les secrets (`DATABASE_URL`) passent par l'environnement du service ou un
gestionnaire de secrets. Le fichier `.env` sert au développement local
uniquement — il est git-ignoré et exclu de l'image (`.dockerignore`).
