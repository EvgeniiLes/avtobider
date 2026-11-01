# Writes the Telegram bot settings into /opt/autobider/.env on the server (reports and alerts to the owner's Telegram).
# Run on your own computer (Windows PowerShell). The bot token is typed hidden and never printed. Press Enter to skip a value.
# 1) In Telegram open @BotFather, send /newbot, choose a name and a username (it must end with "bot"). BotFather gives you the token.
# 2) Run: powershell -ExecutionPolicy Bypass -File deploy\set-telegram-keys.ps1
# NOTE: servers in Russia often cannot reach api.telegram.org. If so, run a small reverse proxy abroad (see docs\deployment.md)
# and give its address as the API base, for example https://tg-relay.example.com
param(
    [string]$Server = "89.111.175.76",
    [string]$Key = (Join-Path $env:USERPROFILE ".ssh\avtobider_ed25519")
)

function Read-Secret($prompt) {
    $secure = Read-Host $prompt -AsSecureString
    return [System.Net.NetworkCredential]::new("", $secure).Password
}

$token = Read-Secret "Bot token from BotFather (TELEGRAM_BOT_TOKEN), Enter to skip"
$name = Read-Host "Bot username without @ (TELEGRAM_BOT_NAME), Enter to skip"
$api = Read-Host "API base if you use a relay (TELEGRAM_API_BASE), Enter to keep https://api.telegram.org"
$lines = @()
if ($token) { $lines += "TELEGRAM_BOT_TOKEN=$token" }
if ($name) { $lines += "TELEGRAM_BOT_NAME=$($name.TrimStart('@'))" }
if ($api) { $lines += "TELEGRAM_API_BASE=$($api.TrimEnd('/'))" }
if (-not $lines) { Write-Host "Nothing entered, nothing changed."; exit 0 }

$tmp = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllText($tmp, (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
try {
    scp -i $Key -o IdentitiesOnly=yes -q $tmp "root@${Server}:/tmp/telegram-keys.txt"
    ssh -i $Key -o IdentitiesOnly=yes "root@$Server" "python3 /opt/autobider/deploy/apply_env.py /tmp/telegram-keys.txt; rm -f /tmp/telegram-keys.txt; systemctl restart autobider autobider-worker; sleep 2; systemctl is-active autobider autobider-worker"
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
