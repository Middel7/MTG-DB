# Mise à jour de la base de PRODUCTION (Render) depuis ce poste.
#
#   .\update-prod.ps1 --skip tags   # run quotidien
#   .\update-prod.ps1 --only tags   # run hebdomadaire des tags
#   .\update-prod.ps1 --dry-run     # plan sans exécution
#
# Identique à update.ps1 à une différence près : les variables de .env.prod
# (git-ignoré) sont exportées AVANT l'appel. Les trois points de chargement du
# .env (src/mtgdb/db/engine.py, alembic/env.py, scripts/import_game_changers.py)
# appellent load_dotenv() SANS override : une variable déjà définie dans
# l'environnement l'emporte donc sur le .env local. C'est ce qui permet de viser
# la prod sans modifier le .env de développement.
#
# ⚠️ Le verrou data/.update_all.lock est commun à TOUS les runs de ce dépôt :
# un run prod lancé pendant un run local sort en code 2 sans rien faire. Les
# horaires des tâches planifiées sont espacés pour cette raison.
#
# Mesure du 2026-08-30 : un run complet vers Render a pris 2 h 19 (dont 2 h 11
# pour la seule étape Scryfall), contre 7 min sur la base locale. Le goulot est
# le disque de l'instance Render, pas la liaison Internet.

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

# psycopg2 refuse le préfixe postgres:// que Render fournit encore dans son
# interface. La correction est purement syntaxique et sans effet de bord.
if ($env:DATABASE_URL.StartsWith("postgres://")) {
    $env:DATABASE_URL = "postgresql://" + $env:DATABASE_URL.Substring("postgres://".Length)
    Write-Host "Prefixe postgres:// corrige en postgresql:// (refuse par psycopg2)."
}

# Garde-fou : sans lui, un .env.prod mal rempli ferait tourner le run "prod"
# sur la base locale, en silence et avec un rapport final tout vert.
if ($env:DATABASE_URL -match "@(localhost|127\.0\.0\.1)[:/]") {
    Write-Error "DATABASE_URL de .env.prod pointe sur localhost : ce n'est pas la prod. Abandon."
    exit 1
}

Write-Host "Cible : PRODUCTION (Render) — variables chargees : $($loaded -join ', ')"
& (Join-Path $root "update.ps1") @args
exit $LASTEXITCODE
