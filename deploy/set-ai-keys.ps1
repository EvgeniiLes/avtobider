# Writes the AI keys (Claude for texts, fal.ai for images and video) into /opt/autobider/.env on the server.
# Run on your own computer (Windows PowerShell). Keys are typed hidden and never printed. Press Enter to skip a key.
# Example: powershell -ExecutionPolicy Bypass -File deploy\set-ai-keys.ps1
param(
    [string]$Server = "89.111.175.76",
    [string]$Key = (Join-Path $env:USERPROFILE ".ssh\avtobider_ed25519")
)

function Read-Secret($prompt) {
    $secure = Read-Host $prompt -AsSecureString
    return [System.Net.NetworkCredential]::new("", $secure).Password
}

$claude = Read-Secret "Claude API key (ANTHROPIC_API_KEY), Enter to skip"
$fal = Read-Secret "fal.ai key (FAL_KEY), Enter to skip"
$lines = @()
if ($claude) { $lines += "ANTHROPIC_API_KEY=$claude" }
if ($fal) { $lines += "FAL_KEY=$fal" }
if (-not $lines) { Write-Host "Nothing entered, nothing changed."; exit 0 }

$tmp = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllText($tmp, (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
try {
    scp -i $Key -o IdentitiesOnly=yes -q $tmp "root@${Server}:/tmp/ai-keys.txt"
    ssh -i $Key -o IdentitiesOnly=yes "root@$Server" "python3 /opt/autobider/deploy/apply_env.py /tmp/ai-keys.txt; rm -f /tmp/ai-keys.txt; systemctl restart autobider autobider-worker; sleep 2; systemctl is-active autobider autobider-worker"
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
