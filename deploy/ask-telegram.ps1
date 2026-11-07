# Вопрос владельцу в Telegram перед выкладкой: бот присылает сообщение с кнопками «Да, сделать» и «Нет», скрипт ждёт ответа.
# Код выхода: 0 нажали «Да», 1 нажали «Нет», 2 ответа нет или Telegram недоступен. Выкладывать можно только при коде 0.
# Пример:
#   powershell -ExecutionPolicy Bypass -File deploy\ask-telegram.ps1 -Text "Выкладываю report-name: статистика Директа. Выкладывать?"
param(
    [Parameter(Mandatory = $true)][string]$Text,
    [string]$Email = "jeki200186@gmail.com",
    [int]$Wait = 600,
    [string]$Server = "89.111.175.76",
    [string]$Key = (Join-Path $env:USERPROFILE ".ssh\avtobider_ed25519")
)
$ErrorActionPreference = "Stop"
# Текст уходит в файл на сервере, а не в командную строку: кавычки и русские буквы не ломаются.
$local = Join-Path ([System.IO.Path]::GetTempPath()) "ask-text.txt"
[System.IO.File]::WriteAllText($local, $Text, (New-Object System.Text.UTF8Encoding($false)))
scp -i $Key -o IdentitiesOnly=yes -o BatchMode=yes -q $local "root@${Server}:/tmp/ask-text.txt"
Remove-Item $local -Force -ErrorAction SilentlyContinue
ssh -i $Key -o IdentitiesOnly=yes -o BatchMode=yes "root@$Server" "chmod 644 /tmp/ask-text.txt && cd /opt/autobider && runuser -u autobider -- /opt/autobider/.venv/bin/python -m app.ask --email '$Email' --wait $Wait ""`$(cat /tmp/ask-text.txt)""; code=`$?; rm -f /tmp/ask-text.txt; exit `$code"
exit $LASTEXITCODE
