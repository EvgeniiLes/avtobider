# Writes Yandex ID login keys into /opt/autobider/.env on the server and restarts the service.
# Run on your own computer (Windows PowerShell). Secrets are typed hidden and never printed.
# Example: powershell -ExecutionPolicy Bypass -File deploy\set-login-keys.ps1 -Domain addtracker.ru
param(
    [string]$Domain = "addtracker.ru",
    [string]$Server = "89.111.175.76",
    [string]$Key = (Join-Path $env:USERPROFILE ".ssh\avtobider_ed25519")
)

$id = Read-Host "Paste ClientID of the LOGIN app (Web)"
$secure = Read-Host "Paste Client secret (input is hidden)" -AsSecureString
$secret = [System.Net.NetworkCredential]::new("", $secure).Password
if (-not $id -or -not $secret) { Write-Host "Empty value, nothing changed."; exit 1 }

# Build the file with LF line endings and no BOM, then copy it to the server.
$text = "YANDEX_LOGIN_CLIENT_ID=$id`nYANDEX_LOGIN_CLIENT_SECRET=$secret`nYANDEX_REDIRECT_URI=https://$Domain/auth/yandex/callback`n"
$tmp = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllText($tmp, $text, (New-Object System.Text.UTF8Encoding($false)))
try {
    scp -i $Key -o IdentitiesOnly=yes -q $tmp "root@${Server}:/tmp/login-keys.txt"
    ssh -i $Key -o IdentitiesOnly=yes "root@$Server" "python3 /opt/autobider/deploy/apply_env.py /tmp/login-keys.txt; rm -f /tmp/login-keys.txt; systemctl restart autobider; sleep 2; systemctl is-active autobider; curl -s -m 5 http://127.0.0.1:8000/api/config"
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
Write-Host ""
Write-Host "If you see yandex_login:true above, the keys are in place."
