# Installe les tâches planifiées Windows de mise à jour de MTG-DB (base LOCALE).
#
#   .\scripts\install_scheduled_tasks.ps1            # installe les tâches locales
#   .\scripts\install_scheduled_tasks.ps1 -Remove    # désinstalle tout, y compris
#                                                    # les anciennes tâches "(prod)"
#
# ── La production ne passe plus par ce poste ────────────────────────────────
#
# Les deux tâches « MTG-DB Update (prod) » et « MTG-DB Tags (prod) » ont été
# retirées : la base Render est désormais alimentée par deux Cron Jobs Render
# qui construisent le Dockerfile du dépôt (voir render.yaml et
# docs/deploiement.md). Un PC éteint, en veille ou déconnecté ne prive plus la
# production de mise à jour — c'était le seul intérêt du montage précédent.
#
# `-Remove` supprime aussi ces deux anciennes tâches si elles traînent encore
# sur la machine : c'est le geste de décommissionnement, à faire APRÈS un
# premier run Render vert, jamais avant.
#
# `update-prod.ps1` reste dans le dépôt comme secours manuel (incident Render,
# rattrapage d'un run échoué), mais n'est plus planifié.
#
# ── Planning ────────────────────────────────────────────────────────────────
#
#   08:00 et 20:00          MTG-DB Update          base locale, --skip tags
#   dimanche 05:00          MTG-DB Tags            base locale, --only tags
#
# Durées mesurées sur la base locale : ~8 min 30 pour un run complet sans les
# tags, quelques secondes si Scryfall n'a rien republié ; ~40 min pour les tags.
#
# Le créneau du dimanche est resté à 05:00. Il avait été déplacé de 03:00 pour
# ne pas croiser le run prod de 02:00, qui n'existe plus ; rien n'impose de le
# ramener en arrière, et le verrou est désormais posé sur la base visée
# (pg_advisory_lock) : un run local et un run prod ne se bloquent plus
# mutuellement, puisqu'ils ne visent pas la même base.
#
# ── Divers ──────────────────────────────────────────────────────────────────
#
# Aucun droit administrateur requis : les tâches tournent sous l'utilisateur
# courant, uniquement lorsqu'il est connecté. -StartWhenAvailable rattrape les
# exécutions manquées si la machine était éteinte à l'heure prévue.

[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$Prod
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$updateScript = Join-Path $root "update.ps1"

$taskUpdate = "MTG-DB Update"
$taskTags = "MTG-DB Tags"
# Conservés pour le seul besoin de les DÉSINSTALLER : ces tâches ne sont plus
# créées par ce script.
$taskUpdateProd = "MTG-DB Update (prod)"
$taskTagsProd = "MTG-DB Tags (prod)"

if ($Prod) {
    Write-Error @"
-Prod n'existe plus : la base de production est alimentee par des Cron Jobs Render
(voir render.yaml et docs/deploiement.md), plus par ce poste.

  Decommissionner les anciennes taches prod : .\scripts\install_scheduled_tasks.ps1 -Remove
  Run manuel de secours vers la prod        : .\update-prod.ps1 --skip tags
"@
    exit 1
}

if ($Remove) {
    foreach ($name in @($taskUpdate, $taskTags, $taskUpdateProd, $taskTagsProd)) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Tache supprimee : $name"
        }
        else {
            Write-Host "Tache absente (rien a faire) : $name"
        }
    }
    return
}

if (-not (Test-Path $updateScript)) {
    Write-Error "Introuvable : $updateScript"
    exit 1
}

function New-Settings([int]$LimitHours) {
    # Rattraper les runs manqués, ne pas s'arrêter sur batterie, et tuer un run
    # qui dépasserait la limite (garde-fou ; le verrou empêche déjà les doublons).
    New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours $LimitHours) `
        -MultipleInstances IgnoreNew
}

# 3 h suffit largement pour la base locale (8 min 30 mesurées, 40 min pour les tags).
$settingsLocal = New-Settings 3

function New-UpdateAction([string]$Script, [string]$Arguments) {
    # -NoProfile : démarrage plus rapide et insensible au profil utilisateur
    New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`" $Arguments" `
        -WorkingDirectory $root
}

# ── Tâche 1 : mise à jour locale 2×/jour, sans les tags ─────────────────────
Register-ScheduledTask `
    -TaskName $taskUpdate `
    -Action (New-UpdateAction $updateScript "--skip tags") `
    -Trigger @(
        (New-ScheduledTaskTrigger -Daily -At 08:00),
        (New-ScheduledTaskTrigger -Daily -At 20:00)
    ) `
    -Settings $settingsLocal `
    -Description "MTG-DB (base locale) : Scryfall + Cardmarket + Game Changers, sans les tags. 2x/jour." `
    -Force | Out-Null
Write-Host "Tache installee : $taskUpdate    08:00 et 20:00   --skip tags"

# ── Tâche 2 : tags Tagger en local, 1×/semaine ──────────────────────────────
Register-ScheduledTask `
    -TaskName $taskTags `
    -Action (New-UpdateAction $updateScript "--only tags") `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 05:00) `
    -Settings $settingsLocal `
    -Description "MTG-DB (base locale) : import des tags Scryfall Tagger. Hebdomadaire (long)." `
    -Force | Out-Null
Write-Host "Tache installee : $taskTags      dimanche 05:00   --only tags"

Write-Host ""
Write-Host "La base de PRODUCTION est alimentee par Render (render.yaml), pas par ce poste."
Write-Host ""
Write-Host "Verifier         : Get-ScheduledTask -TaskName 'MTG-DB*'"
Write-Host "Lancer a la main  : Start-ScheduledTask -TaskName '$taskUpdate'"
Write-Host "Dernier resultat  : Get-ScheduledTaskInfo -TaskName 'MTG-DB*' | Select TaskName,LastRunTime,LastTaskResult"
Write-Host "Desinstaller      : .\scripts\install_scheduled_tasks.ps1 -Remove"
