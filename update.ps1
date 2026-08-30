# Mise à jour complète de la base MTG-DB.
#
#   .\update.ps1                    # tout mettre à jour
#   .\update.ps1 --only cardmarket  # une seule source
#   .\update.ps1 --tags-all         # retraiter tous les tags (long)
#   .\update.ps1 --dry-run          # afficher le plan sans exécuter
#
# Tous les arguments sont transmis tels quels à scripts/update_all.py.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "Environnement virtuel introuvable : $python`nCrée-le avec : uv venv  (ou python -m venv .venv)"
    exit 1
}

$env:PYTHONIOENCODING = "utf-8"
& $python (Join-Path $root "scripts\update_all.py") @args
exit $LASTEXITCODE
