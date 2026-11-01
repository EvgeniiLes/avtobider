# Выкладка обновления на сервер: сначала копия базы и архив прежнего кода, потом файлы, перезапуск и проверка.
# Запускать на своём компьютере (Windows PowerShell) из корня проекта. Откат: deploy\rollback.ps1.
# Пример:
#   powershell -ExecutionPolicy Bypass -File deploy\release.ps1 -Name bids -Files app/direct/bids.py,app/web/app.js
# -DryRun только проверяет файлы и показывает, что будет сделано, ничего на сервере не трогая.
param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string[]]$Files,
    [int]$Keep = 10,
    [switch]$DryRun,
    [string]$Server = "89.111.175.76",
    [string]$Key = (Join-Path $env:USERPROFILE ".ssh\avtobider_ed25519")
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$remoteRoot = "/opt/autobider"
$ssh = @("-i", $Key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes")

if ($Name -notmatch '^[a-z0-9-]+$') { throw "Название выкладки: только строчная латиница, цифры и дефис." }

# Файлы: только внутри app/, существуют, пути с косой чертой вперёд.
$list = @()
foreach ($file in ($Files | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() })) {   # при запуске через -File список приходит одной строкой через запятую
    $relative = ($file -replace '\\', '/').TrimStart('./')
    if ($relative -notmatch '^app/') { throw "Выкладываем только файлы из app/: $file" }
    if (-not (Test-Path (Join-Path $root $relative) -PathType Leaf)) { throw "Нет такого файла: $file" }
    $list += $relative
}

# Выкладка должна соответствовать коммиту: иначе откатываться потом не к чему.
Push-Location $root
try {
    $commit = (git rev-parse --short HEAD).Trim()
    $dirty = @(git status --porcelain -- $list)
} finally { Pop-Location }
if ($dirty.Count -gt 0) { Write-Warning "Есть правки, не сохранённые в git ($($dirty.Count) файл.): сначала закоммитьте, чтобы выкладка соответствовала коммиту $commit." }

Write-Host "Выкладка «$Name», коммит $commit, файлов: $($list.Count)"
$list | ForEach-Object { Write-Host "  $_" }
if ($DryRun) { Write-Host "Проверка завершена, на сервере ничего не менялось (-DryRun)."; exit 0 }

function Invoke-Native([string]$what, [scriptblock]$run) {
    & $run
    if ($LASTEXITCODE -ne 0) { throw "$what не удалось (код $LASTEXITCODE). Выкладка остановлена, прежняя версия на сервере не тронута или сохранена в backup." }
}

# 1. Копии: база и прежний код.
# Скрипт копий должен уйти с переводами строк LF, даже если git на Windows положил CRLF.
$pre = Join-Path ([System.IO.Path]::GetTempPath()) "pre-release.sh"
[System.IO.File]::WriteAllText($pre, ([System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "pre-release.sh")) -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding($false)))
Invoke-Native "Загрузка скрипта копий" { scp @ssh -q $pre "root@${Server}:/tmp/pre-release.sh" }
Remove-Item $pre -Force -ErrorAction SilentlyContinue
Invoke-Native "Копия базы и прежнего кода" { ssh @ssh "root@$Server" "bash /tmp/pre-release.sh $Name $Keep" }

# 2. Файлы.
foreach ($relative in $list) {
    Invoke-Native "Копирование $relative" { scp @ssh -q (Join-Path $root $relative) "root@${Server}:$remoteRoot/$relative" }
}

# 3. Права, перезапуск, проверка, запись в журнал выкладок.
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
$log = "$stamp | $Name | $commit | files: $($list.Count)"
$check = "chown -R autobider:autobider $remoteRoot/app && systemctl restart autobider autobider-worker && sleep 4 && systemctl is-active autobider autobider-worker && curl -s -o /dev/null -w 'site %{http_code}\n' http://127.0.0.1:8000/ && echo '$log' >> $remoteRoot/backup/releases.log"
Invoke-Native "Перезапуск и проверка" { ssh @ssh "root@$Server" $check }
Write-Host "Готово. Откат: powershell -ExecutionPolicy Bypass -File deploy\rollback.ps1 -Name $Name"
