# Writes the Yandex app keys (Direct + Metrika access) into /opt/autobider/.env on the server and restarts services.
# Run on your own computer (Windows PowerShell). The secret is typed hidden and never printed.
# Example: powershell -ExecutionPolicy Bypass -File deploy\set-direct-keys.ps1 -Domain addtracker.ru
param(
    [string]$Domain = "addtracker.ru",
    [string]$Server = "89.111.175.76",
    [string]$Key = (Join-Path $env:USERPROFILE ".ssh\avtobider_ed25519")
)

$id = Read-Host "Paste ClientID of the API app (type: for API access)"
$secure = Read-Host "Paste Client secret of that app (input is hidden)" -AsSecureString
$secret = [System.Net.NetworkCredential]::new("", $secure).Password
if (-not $id -or -not $secret) { Write-Host "Empty value, nothing changed."; exit 1 }

$text = "YANDEX_CLIENT_ID=$id`nYANDEX_CLIENT_SECRET=$secret`n"
$tmp = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllText($tmp, $text, (New-Object System.Text.UTF8Encoding($false)))
try {
    scp -i $Key -o IdentitiesOnly=yes -q $tmp "root@${Server}:/tmp/direct-keys.txt"
    ssh -i $Key -o IdentitiesOnly=yes "root@$Server" "python3 /opt/autobider/deploy/apply_env.py /tmp/direct-keys.txt; rm -f /tmp/direct-keys.txt; systemctl restart autobider autobider-worker; sleep 2; systemctl is-active autobider autobider-worker; curl -s -m 5 http://127.0.0.1:8000/api/config"
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
Write-Host ""
Write-Host "If you see direct_oauth:true above, the keys are in place."
