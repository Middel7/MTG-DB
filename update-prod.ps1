# Rattrapage MANUEL de la base de PRODUCTION (Render) depuis ce poste.
#
#   .\update-prod.ps1 --skip tags   # rejouer le run quotidien
#   .\update-prod.ps1 --only tags   # rejouer le run hebdomadaire des tags
#   .\update-prod.ps1 --dry-run     # plan sans exécution
#
# ── Ce script n'est plus le montage nominal ─────────────────────────────────
#
# La production est alimentée par deux Cron Jobs Render qui exécutent le
# Dockerfile de ce dépôt (voir render.yaml et docs/deploiement.md). Plus aucune
# tâche planifiée Windows ne vise la prod.
#
# Ce script reste pour les deux seuls cas où il sert encore :
#
#   - incident Render (job en échec, plateforme indisponible, build cassé) ;
#   - rattrapage immédiat quand on ne veut pas attendre le créneau de 01:00 UTC.
#
# Il sera plus lent qu'un run Render : ~9 min de latence réseau et de transfert
# s'ajoutent, mesurés (RTT SQL médian 22 ms, upload 20-25 Mbit/s), et le lien
# WAN peut lâcher en cours de route — c'est ce qui a fait échouer le run du
# 17/09 après 78 minutes de travail.
#
# ── Ce qu'il fait, et ce qu'il ne fait plus ─────────────────────────────────
#
# Il charge .env.prod (git-ignoré) et exporte ses variables avant d'appeler
# update.ps1. Cela fonctionne parce que les trois points de chargement du .env
# — src/mtgdb/db/engine.py, alembic/env.py, scripts/import_game_changers.py —
# appellent load_dotenv() SANS override : une variable déjà définie dans
# l'environnement l'emporte sur le .env de développement.
#
# Les deux garde-fous qu'il portait autrefois vivent désormais en Python, donc
# sur tous les chemins d'exécution, conteneur compris :
#
#   postgres:// → postgresql://   mtgdb.db.urls.normalize_database_url(),
#                                 appelée par les trois points de chargement ;
#   refus de localhost            mtgdb.db.engine.assert_remote_database(),
#                                 appelée par scripts/update_all.py.
#
# Ce script se contente d'armer le second en posant MTGDB_REQUIRE_REMOTE_DB :
# hors conteneur, rien ne distingue autrement un run « prod » d'un run local.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$envFile = Join-Path $root ".env.prod"

if (-not (Test-Path $envFile)) {
    Write-Error "Fichier introuvable : $envFile`nCopie .env.prod.example en .env.prod et renseigne DATABASE_URL (base Render)."
    exit 1
}

# Lecture volontairement minimaliste : KEY=VALUE, commentaires et lignes vides
# ignorés. Toutes les clés sont exportées, pas seulement DATABASE_URL — c'est ce
# qui permet de piloter SKIP_SCRYFALL_PRICES depuis ce même fichier.
$loaded = @()
foreach ($line in Get-Content $envFile) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
    $i = $trimmed.IndexOf("=")
    if ($i -lt 1) { continue }
    $key = $trimmed.Substring(0, $i).Trim()
    $value = $trimmed.Substring($i + 1).Trim().Trim('"').Trim("'")
    Set-Item -Path "Env:$key" -Value $value
    $loaded += $key
}

if (-not $env:DATABASE_URL) {
    Write-Error "DATABASE_URL absent ou vide dans $envFile."
    exit 1
}

# Arme le garde-fou côté Python : un .env.prod mal rempli qui pointerait sur la
# base locale doit faire échouer le run AVANT la première écriture, plutôt que
# de produire un rapport final tout vert sur la mauvaise base.
$env:MTGDB_REQUIRE_REMOTE_DB = "1"

Write-Host "Cible : PRODUCTION (Render), rattrapage manuel — variables chargees : $($loaded -join ', ')"
& (Join-Path $root "update.ps1") @args
exit $LASTEXITCODE
