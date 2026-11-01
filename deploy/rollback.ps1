# Откат кода на сервере к версии, сохранённой перед выкладкой (backup/app-before-<название>.tar.gz).
# База не трогается: новые таблицы и поля только добавляются, старый код с новой базой работает.
# Прежняя (откатываемая) версия остаётся в /opt/autobider/app.rolled-back, чтобы её можно было разобрать.
# Пример:
#   powershell -ExecutionPolicy Bypass -File deploy\rollback.ps1 -Name bids
# Без -Name показывает, какие версии есть. -DryRun только проверяет, что архив существует.
param(
    [string]$Name = "",
    [switch]$DryRun,
    [string]$Server = "89.111.175.76",
    [string]$Key = (Join-Path $env:USERPROFILE ".ssh\avtobider_ed25519")
)
$ErrorActionPreference = "Stop"
$ssh = @("-i", $Key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes")

if (-not $Name) {
    ssh @ssh "root@$Server" "ls -1t /opt/autobider/backup/app-before-*.tar.gz 2>/dev/null; echo ---; tail -n 10 /opt/autobider/backup/releases.log 2>/dev/null"
    Write-Host "Выберите название из app-before-<название>.tar.gz и запустите: deploy\rollback.ps1 -Name <название>"
    exit 0
}
if ($Name -notmatch '^[a-z0-9-]+$') { throw "Название: только строчная латиница, цифры и дефис." }

$archive = "/opt/autobider/backup/app-before-$Name.tar.gz"
ssh @ssh "root@$Server" "test -f $archive"
if ($LASTEXITCODE -ne 0) { throw "На сервере нет архива $archive. Список: запустите без -Name." }
if ($DryRun) { Write-Host "Архив есть, откат возможен (-DryRun, ничего не менялось)."; exit 0 }

$remote = "set -e; cd /opt/autobider; systemctl stop autobider autobider-worker; rm -rf app.rolled-back; mv app app.rolled-back; " +
          "tar -xzf $archive -C /opt/autobider; chown -R autobider:autobider app; systemctl start autobider autobider-worker; sleep 4; " +
          "systemctl is-active autobider autobider-worker; curl -s -o /dev/null -w 'site %{http_code}\n' http://127.0.0.1:8000/; " +
          "echo `"`$(date '+%Y-%m-%d %H:%M') | rollback to $Name`" >> backup/releases.log"
ssh @ssh "root@$Server" $remote
if ($LASTEXITCODE -ne 0) { throw "Откат не завершился (код $LASTEXITCODE). Прежняя версия лежит в /opt/autobider/app.rolled-back." }
Write-Host "Откат выполнен: код как перед выкладкой «$Name». Откатываемая версия сохранена в /opt/autobider/app.rolled-back."
