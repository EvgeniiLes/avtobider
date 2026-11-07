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
$ssh = @("-i", $Key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes")
$utf8 = New-Object System.Text.UTF8Encoding($false)
# Текст и запускающий скрипт уходят файлами: кавычки и русские буквы в командной строке ssh ломаются.
$textFile = Join-Path ([System.IO.Path]::GetTempPath()) "ask-text.txt"
$runner = Join-Path ([System.IO.Path]::GetTempPath()) "ask-run.sh"
[System.IO.File]::WriteAllText($textFile, $Text, $utf8)
[System.IO.File]::WriteAllText($runner, "cd /opt/autobider`nrunuser -u autobider -- /opt/autobider/.venv/bin/python -m app.ask --email '$Email' --wait $Wait `"`$(cat /tmp/ask-text.txt)`"`ncode=`$?`nrm -f /tmp/ask-text.txt /tmp/ask-run.sh`nexit `$code`n", $utf8)
scp @ssh -q $textFile "root@${Server}:/tmp/ask-text.txt"
scp @ssh -q $runner "root@${Server}:/tmp/ask-run.sh"
Remove-Item $textFile, $runner -Force -ErrorAction SilentlyContinue
ssh @ssh "root@$Server" "chmod 644 /tmp/ask-text.txt && bash /tmp/ask-run.sh"
exit $LASTEXITCODE
