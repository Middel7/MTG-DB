# Déploiement & mise à jour automatique

La logique de mise à jour vit **entièrement dans `scripts/update_all.py`**. Les
planificateurs (Planificateur Windows, cron, Ofelia, Kubernetes) ne font que
*déclencher* ce script : changer d'ordonnanceur ou d'hébergement ne demande donc
aucune réécriture.

---

## Garanties du script

| Garantie | Détail |
|---|---|
| **Verrou anti-chevauchement** | Deux runs simultanés sont impossibles. Le 2ᵉ sort en **code 2** sans rien faire. Un verrou dont le processus est mort (ou vieux de plus de 6 h) est repris automatiquement — un crash ne bloque pas les runs suivants. |
| **Idempotence** | Un bulk Scryfall déjà importé n'est **ni retéléchargé ni re-parsé** (comparaison de `import_runs.source_file`). Un run répété sans nouveauté coûte quelques secondes au lieu de 7 minutes et 2,4 Go. |
| **Purge automatique** | Les anciens bulks (2,4 Go pièce) sont supprimés après import. `--keep-bulks N` pour en conserver davantage, `--no-purge` pour désactiver. |
| **Journalisation** | Chaque run écrit `logs/update_<horodatage>.log` (sans les barres de progression). Les 30 derniers sont conservés. |
| **Codes de sortie** | `0` succès · `1` au moins une étape en échec · `2` un run est déjà en cours. Exploitable par n'importe quel superviseur. |

---

## Cadence recommandée

Scryfall republie son bulk `all_cards` **environ toutes les 12 h** (constaté :
deux bulks le 12/07, à 09h28 et 21h31). Un rythme de **2 runs par jour** est donc
justifié — il ne s'agit pas seulement de robustesse, mais bien de fraîcheur.

L'étape **tags** est traitée à part : elle dure ~40 min et retente à chaque passage
les cartes dépourvues de tags côté Tagger. La lancer quotidiennement coûterait cher
pour presque rien.

| Tâche | Fréquence | Commande | Durée |
|---|---|---|---|
| Mise à jour | 2×/jour (08:00, 20:00) | `update_all.py --skip tags` | ~7 min, ou quelques secondes s'il n'y a rien de neuf |
| Tags | 1×/semaine (dimanche 05:00) | `update_all.py --only tags` | ~40 min |
| Mise à jour **prod** | 1×/jour (02:00) | `update-prod.ps1 --skip tags` | **2 h 19** mesuré |
| Tags **prod** | 1×/semaine (dimanche 22:00) | `update-prod.ps1 --only tags` | long |

---

## Windows (poste local)

```powershell
.\scripts\install_scheduled_tasks.ps1          # installer
.\scripts\install_scheduled_tasks.ps1 -Remove  # désinstaller
```

Aucun droit administrateur requis. Les tâches tournent sous l'utilisateur courant,
lorsqu'il est connecté ; `-StartWhenAvailable` rattrape les exécutions manquées si
la machine était éteinte à l'heure prévue.

```powershell
Get-ScheduledTask -TaskName 'MTG-DB*'                    # vérifier
Start-ScheduledTask -TaskName 'MTG-DB Update'            # lancer maintenant
Get-ScheduledTaskInfo -TaskName 'MTG-DB Update'          # dernier résultat
```

`LastTaskResult` vaut `0` en cas de succès, `1` si une étape a échoué, `2` si un run
était déjà en cours.

---

## Windows → base de production Render (en service)

C'est le montage réellement en place. Les imports tournent sur le poste de Fabien
et écrivent dans la base Render `relictrade` : c'est la recommandation de
`rapport_audit_hebergement.txt` (§ Décision 2), tout le coût étant côté *lecture
Scryfall* (392 Mo par run) et non côté *écriture PostgreSQL* (~34 Mo).

```powershell
.\scripts\install_scheduled_tasks.ps1 -Prod   # installe locale + prod
.\update-prod.ps1 --skip tags                 # run manuel vers la prod
```

`update-prod.ps1` charge `.env.prod` (git-ignoré) et exporte ses variables avant
d'appeler `update.ps1`. Cela fonctionne parce que les trois points de chargement du
`.env` — `src/mtgdb/db/engine.py`, `alembic/env.py`, `scripts/import_game_changers.py` —
appellent `load_dotenv()` **sans `override`** : une variable déjà présente dans
l'environnement l'emporte. Le `.env` de développement n'est donc jamais modifié.

Trois garde-fous dans `update-prod.ps1` :

| Garde-fou | Motif |
|---|---|
| Refus si `.env.prod` est absent | Sans lui, le run tomberait silencieusement sur la base locale. |
| `postgres://` → `postgresql://` | Render fournit encore l'ancien préfixe, que `psycopg2` refuse. |
| Refus si l'URL contient `localhost` | Un `.env.prod` mal rempli produirait un rapport final tout vert… sur la mauvaise base. |

### Durées mesurées (2026-08-30)

| Étape | Local | Prod Render |
|---|---|---|
| Scryfall | 6 min 24 | **2 h 10** |
| Cardmarket | 29 s | 8 min 24 |
| Game Changers | 0 s | 42 s |
| **Total** | **6 min 54** | **2 h 19** |

Le goulot n'est **pas** la liaison Internet : l'inspection de `pg_stat_activity`
pendant le run montre les `INSERT` en attente sur `IO/DataFileRead`, c'est-à-dire le
disque de l'instance Render. Cohérent avec l'audit (`shared_buffers` 64 Mo pour des
tables de plusieurs centaines de Mo).

C'est ce qui justifie **une seule mise à jour quotidienne en prod** contre deux en
local, et une `ExecutionTimeLimit` portée à 6 h sur les tâches prod : à 3 h, un run
un peu lent serait tué en plein travail.

### Espacement des créneaux

Le verrou `data/.update_all.lock` est commun à **tous** les runs du dépôt : deux runs
qui se chevauchent, et le second sort en code 2 sans rien faire. D'où :

```
02:00 tous les jours   prod   --skip tags     (jusqu'à ~04:20)
08:00 et 20:00         local  --skip tags     (~7 min)
dimanche 05:00         local  --only tags     (~40 min)
dimanche 22:00         prod   --only tags
```

Les tags locaux sont passés de 03:00 à 05:00 : à 03:00, le run prod lancé à 02:00
tourne encore.

### SKIP_SCRYFALL_PRICES en prod

`.env.prod` contient `SKIP_SCRYFALL_PRICES=1`. Motif vérifié dans le dépôt
consommateur : `apps/api/AGENTS.md` de RELIC-Trade **interdit** l'usage de
`scryfall_card_prices` (« Ne jamais utiliser […] pour un prix dans ce projet »), les
prix venant de `cardmarket_price_guide_entries`. Les écrire coûtait ~344 000 lignes
par run pour rien. Aucune donnée existante n'est supprimée : on cesse d'écrire.

### Migrations

Le run **n'applique pas** les migrations. Avant de déployer un changement de schéma :

```powershell
$env:DATABASE_URL = ((Get-Content .env.prod | Where-Object { $_ -like 'DATABASE_URL=*' }) -replace '^DATABASE_URL=','')
.venv\Scripts\python.exe -m alembic upgrade head
```

⚠️ `relictrade` héberge **deux** chaînes Alembic : `mtgdb_alembic_version` (ce dépôt)
et `mtgtrade_alembic_version` (RELIC-Trade). Les garde-fous d'`alembic/env.py`
(`version_table` + `include_object`) sont indispensables. Contrairement à la base
locale `manamind`, ManaMind_AI n'est **pas** présent en prod.

---

## Docker (production)

Le conteneur est **one-shot** : il fait le job puis s'arrête. Il n'y a
volontairement **pas de cron à l'intérieur** — un conteneur qui tourne 24/7 pour ne
rien faire 99 % du temps est un anti-pattern, et cron n'hérite pas des variables
d'environnement Docker (piège classique : `DATABASE_URL` introuvable au
déclenchement).

```bash
export DATABASE_URL=postgresql://user:pass@postgres:5432/manamind

docker compose build updater
docker compose run --rm updater --dry-run     # vérifier le plan
docker compose run --rm updater --skip tags   # run réel
```

Le volume `scryfall_raw` est **indispensable** : sans lui, chaque run retéléchargerait
2,4 Go depuis zéro.

Si PostgreSQL tourne dans un autre `docker-compose`, rattacher son réseau (voir le
bloc `networks` commenté dans `docker-compose.yml`).

### Planification par le cron de l'hôte

```cron
# Mise à jour 2×/jour (sans les tags)
0 8,20 * * *  cd /opt/mtg-db && docker compose run --rm updater --skip tags >> /var/log/mtgdb.log 2>&1

# Tags, une fois par semaine
0 3 * * 0     cd /opt/mtg-db && docker compose run --rm updater --only tags >> /var/log/mtgdb.log 2>&1
```

⚠️ Le planning vit alors sur l'hôte, pas dans le dépôt : un serveur réinstallé sans
sa crontab cesse de se mettre à jour **silencieusement**. Superviser le code de sortie
(ou l'âge de `max(import_runs.finished_at)`) plutôt que de supposer que ça tourne.

### Alternative : planning versionné dans le dépôt

Pour éviter ce piège, un conteneur ordonnanceur type
[Ofelia](https://github.com/mcuadros/ofelia) déclare le planning directement dans le
`docker-compose.yml`. Sur Kubernetes, l'équivalent est un `CronJob`. Dans les deux cas,
**la même image** est utilisée : le choix de l'ordonnanceur reste réversible.

---

## Supervision

Requête utile pour détecter un décrochage (aucun import réussi depuis > 24 h) :

```sql
SELECT max(finished_at) AS dernier_import_reussi
FROM import_runs
WHERE source = 'scryfall' AND status = 'success';
```

Les secrets (`DATABASE_URL`) passent par l'environnement ou un gestionnaire de secrets.
Le fichier `.env` sert au développement local uniquement — il est git-ignoré et exclu
de l'image (`.dockerignore`).
