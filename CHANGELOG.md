# Journal des modifications

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/).
Les dates sont au format AAAA-MM-JJ.

---

## [Non publié] — 2026-09-19

### La production ne dépend plus d'un poste

La base Render est désormais alimentée par **deux Cron Jobs Render** qui
construisent le `Dockerfile` de ce dépôt (`render.yaml`). Les tâches planifiées
Windows qui visaient la production sont retirées ; les runs locaux vers la base
locale sont conservés tels quels.

Ce que cela règle : un PC éteint, en veille ou déconnecté ne prive plus la
production de mise à jour, et le lien WAN n'est plus un point de panne — le run
du 17/09 s'était interrompu après 78 minutes sur un
`SSL connection has been closed unexpectedly`.

Ce que cela ne règle pas, mesures à l'appui : **la durée**. Un run continuera de
prendre ~100 min. Le goulot est l'instance PostgreSQL (256 Mo de RAM, 0,1 vCPU
pour une base de 3,4 Go), pas la liaison — la part réseau et latence ne pèse que
~9 min sur 110. Voir `docs/deploiement.md`.

### Ajouté

- `render.yaml` : blueprint des deux cron jobs (`mtgdb-catalogue-quotidien`,
  quotidien à 01:00 UTC, `--skip tags` ; `mtgdb-tags-hebdomadaire`, dimanche à
  05:00 UTC, `--only tags`). Région `frankfurt`, imposée par l'URL interne de la
  base.
- `src/mtgdb/db/urls.py` : normalisation `postgres://` → `postgresql://`,
  détection d'une base locale, masquage du mot de passe dans les journaux.
- `src/mtgdb/db/lock.py` : verrou `pg_advisory_lock` avec heartbeat sur la
  connexion porteuse.
- `src/mtgdb/runtime.py` : détection de l'exécution en conteneur.
- `mtgdb.db.engine.assert_remote_database()` : refus d'une base locale quand le
  contexte exige une base distante.
- `tests/` : 47 tests (pytest). Ceux qui portent sur le verrou sont marqués
  `integration` et demandent une base ; ils n'écrivent rien.
- `CHANGELOG.md` (ce fichier).

### Modifié

- **Verrou anti-chevauchement** : `data/.update_all.lock` remplacé par
  `pg_advisory_lock`. Le verrou porte désormais sur la **base visée**, pas sur
  le dossier du dépôt. Deux conséquences : un run venu d'une autre machine est
  vu (indispensable pendant la transition poste → cloud), et un run local vers
  `manamind` ne bloque plus un run vers `relictrade`. Le code de sortie **2**
  est conservé. Plus de verrou périmé à nettoyer : PostgreSQL le libère quand la
  session tombe.
- **Normalisation de `DATABASE_URL`** : déplacée d'`update-prod.ps1` vers les
  **trois** points de lecture Python — `mtgdb.db.engine`, `alembic/env.py` et
  `scripts/import_game_changers.py`, ce dernier lisant `os.environ`
  directement. Sans cela, le premier run en conteneur échouait sur une URL
  `postgres://` fournie par Render.
- **Garde-fou « pas de base locale »** : déplacé d'`update-prod.ps1` vers
  `assert_remote_database()`, appelé par `update_all.py` avant la première
  écriture. Actif en conteneur, ou hors conteneur avec
  `MTGDB_REQUIRE_REMOTE_DB=1` (posé par `update-prod.ps1`).
  `MTGDB_ALLOW_LOCAL_DB=1` lève la restriction.
- **Journalisation** : en conteneur, plus de fichier dans `logs/` — écrire sur
  un disque éphémère revient à écrire dans le vide. Tout part sur stdout, que
  Render capture.
- **Dockerfile** : `ENTRYPOINT` remplacé par
  `CMD ["python", "scripts/update_all.py", "--skip", "tags"]`. Render ne sait
  remplacer que le `CMD` ; avec un `ENTRYPOINT`, `render.yaml` aurait porté un
  `dockerCommand: --skip tags` dont le sens dépend d'une ligne invisible.
  Ajout de `ENV MTGDB_CONTAINER=1`. `logs/` n'est plus créé dans l'image.
- **`docker-compose.yml`** : les variantes s'écrivent en commande complète
  (`docker compose run --rm updater python scripts/update_all.py --only tags`),
  conséquence du passage à `CMD`. Montage de `./logs` retiré.
- **`scripts/install_scheduled_tasks.ps1`** : n'installe plus que les deux
  tâches locales. `-Prod` renvoie désormais un message d'orientation. `-Remove`
  supprime toujours les anciennes tâches `(prod)`, pour le décommissionnement.
- **`update-prod.ps1`** : conservé comme **secours manuel** (incident Render,
  rattrapage immédiat), plus jamais planifié. Ses deux garde-fous vivent
  maintenant en Python ; il se contente de charger `.env.prod` et de poser
  `MTGDB_REQUIRE_REMOTE_DB=1`.
- **`pyproject.toml`** : groupe de dépendances `dev` (pytest) et configuration
  pytest. Le `uv sync --frozen --no-dev` du Dockerfile l'exclut de l'image.
- **`docs/deploiement.md`** : réécrit. Il décrivait un montage poste → Render
  qui n'existe plus.
- **`README.md`** : sections « Mise à jour automatique », « Tests » et
  « Structure du projet ».

### Notes

- **Aucune donnée de prix n'a été touchée.** `scryfall_card_prices` contient
  4 061 065 lignes du 07/06 au 30/08/2026 (738 Mo) : l'écriture est arrêtée
  depuis la mise en place de `SKIP_SCRYFALL_PRICES`, la table est **gelée**, pas
  vide. Cet historique n'est pas reconstituable.
- `uv sync --group dev` a retiré `pillow` du venv local : le paquet n'était
  déclaré nulle part et n'est importé par aucun fichier du dépôt.
- Le build de l'image n'a pas pu être vérifié localement (Docker absent de la
  machine) ; c'est Render qui le validera au premier déploiement.
