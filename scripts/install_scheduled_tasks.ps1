# Installe les tâches planifiées Windows de mise à jour de MTG-DB.
#
#   .\scripts\install_scheduled_tasks.ps1            # base locale uniquement
#   .\scripts\install_scheduled_tasks.ps1 -Prod      # locale + production (Render)
#   .\scripts\install_scheduled_tasks.ps1 -Remove    # désinstalle tout
#
# ── Planning ────────────────────────────────────────────────────────────────
#
#   02:00 tous les jours    MTG-DB Update (prod)   base Render, --skip tags
#   08:00 et 20:00          MTG-DB Update          base locale, --skip tags
#   dimanche 05:00          MTG-DB Tags            base locale, --only tags
#   dimanche 22:00          MTG-DB Tags (prod)     base Render, --only tags
#
# ── Pourquoi ces horaires ───────────────────────────────────────────────────
#
# Le verrou data/.update_all.lock est COMMUN à tous les runs du dépôt : deux runs
# qui se chevauchent et le second sort en code 2 sans rien faire. Les créneaux
# sont donc espacés en fonction des durées MESURÉES, pas estimées :
#
#   run local : ~7 min (quelques secondes si Scryfall n'a rien republié)
#   run prod  : 2 h 19 le 2026-08-30 (dont 2 h 11 pour la seule étape Scryfall)
#
# La prod tourne à 02:00 pour deux raisons : le site n'est pas sollicité à cette
# heure — l'instance Render est lente, autant ne pas la disputer aux visiteurs —
# et le run a jusqu'à 08:00 pour finir avant le premier run local.
#
# Les tags locaux ont été déplacés de 03:00 à 05:00 : à 03:00 le run prod démarré
# à 02:00 est encore en cours, et l'un des deux serait sauté.
#
# Une seule mise à jour quotidienne en prod, contre deux en local : à 2 h 19 le
# run, deux passages coûteraient cher pour rien — le Price Guide Cardmarket n'est
# republié qu'une fois par jour.
#
# ── Divers ──────────────────────────────────────────────────────────────────
#
# Aucun droit administrateur requis : les tâches tournent sous l'utilisateur
# courant, uniquement lorsqu'il est connecté. -StartWhenAvailable rattrape les
# exécutions manquées si la machine était éteinte à l'heure prévue — utile pour
# le créneau de 02:00.

[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$Prod
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$updateScript = Join-Path $root "update.ps1"
$updateProdScript = Join-Path $root "update-prod.ps1"

$taskUpdate = "MTG-DB Update"
$taskTags = "MTG-DB Tags"
$taskUpdateProd = "MTG-DB Update (prod)"
$taskTagsProd = "MTG-DB Tags (prod)"

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

# 3 h suffit largement en local (7 min mesurées). En prod il faut de la marge :
# le run mesuré a pris 2 h 19 et l'instance Render est irrégulière — une limite
# à 3 h tuerait un run un peu lent au milieu de son travail.
$settingsLocal = New-Settings 3
$settingsProd = New-Settings 6

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
Write-Host "Tache installee : $taskUpdate          08:00 et 20:00   --skip tags"

# ── Tâche 2 : tags Tagger en local, 1×/semaine ──────────────────────────────
# 05:00 et non 03:00 : a 03:00 le run prod lance a 02:00 tourne encore.
Register-ScheduledTask `
    -TaskName $taskTags `
    -Action (New-UpdateAction $updateScript "--only tags") `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 05:00) `
    -Settings $settingsLocal `
    -Description "MTG-DB (base locale) : import des tags Scryfall Tagger. Hebdomadaire (long)." `
    -Force | Out-Null
Write-Host "Tache installee : $taskTags             dimanche 05:00   --only tags"

# ── Tâches 3 et 4 : les mêmes, vers la base de production ───────────────────
if ($Prod) {
    if (-not (Test-Path $updateProdScript)) {
        Write-Error "Introuvable : $updateProdScript"
        exit 1
    }
    if (-not (Test-Path (Join-Path $root ".env.prod"))) {
        Write-Error ".env.prod absent : les taches prod echoueraient a chaque run.`nCopie .env.prod.example en .env.prod et renseigne DATABASE_URL."
        exit 1
    }

    Register-ScheduledTask `
        -TaskName $taskUpdateProd `
        -Action (New-UpdateAction $updateProdScript "--skip tags") `
        -Trigger (New-ScheduledTaskTrigger -Daily -At 02:00) `
        -Settings $settingsProd `
        -Description "MTG-DB (base Render) : Scryfall + Cardmarket + Game Changers, sans les tags. 1x/nuit a 02:00 (run mesure a 2h19)." `
        -Force | Out-Null
    Write-Host "Tache installee : $taskUpdateProd   02:00 chaque nuit  --skip tags"

    Register-ScheduledTask `
        -TaskName $taskTagsProd `
        -Action (New-UpdateAction $updateProdScript "--only tags") `
        -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 22:00) `
        -Settings $settingsProd `
        -Description "MTG-DB (base Render) : import des tags Scryfall Tagger. Hebdomadaire (long)." `
        -Force | Out-Null
    Write-Host "Tache installee : $taskTagsProd     dimanche 22:00   --only tags"
}
else {
    Write-Host ""
    Write-Host "Taches PROD non installees. Relance avec -Prod pour alimenter aussi la base Render."
}

Write-Host ""
Write-Host "Verifier         : Get-ScheduledTask -TaskName 'MTG-DB*'"
Write-Host "Lancer a la main  : Start-ScheduledTask -TaskName '$taskUpdateProd'"
Write-Host "Dernier resultat  : Get-ScheduledTaskInfo -TaskName 'MTG-DB*' | Select TaskName,LastRunTime,LastTaskResult"
Write-Host "Desinstaller      : .\scripts\install_scheduled_tasks.ps1 -Remove"
