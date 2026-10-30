$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $projectRoot "backend"
$envPath = Join-Path $backendDir ".env"
$examplePath = Join-Path $backendDir ".env.example"

if (-not (Test-Path $backendDir)) {
    throw "Could not find the backend folder next to this script."
}

if (-not (Test-Path $envPath)) {
    if (-not (Test-Path $examplePath)) {
        throw "backend\.env and backend\.env.example are both missing."
    }
    Copy-Item $examplePath $envPath
    Write-Host "Created backend\.env from backend\.env.example."
}

# Change ONLY the Supabase enable switch. API keys and every other setting are preserved.
$content = Get-Content -LiteralPath $envPath -Raw
$pattern = '(?m)^[ \t]*SUPABASE_ENABLED[ \t]*=.*$'
if ([regex]::IsMatch($content, $pattern)) {
    $content = [regex]::Replace($content, $pattern, 'SUPABASE_ENABLED=false')
} else {
    $content = $content.TrimEnd() + "`r`nSUPABASE_ENABLED=false`r`n"
}
[System.IO.File]::WriteAllText($envPath, $content, [System.Text.UTF8Encoding]::new($false))

Write-Host ""
Write-Host "NEXA local authentication mode is now configured."
Write-Host "SUPABASE_ENABLED=false"
Write-Host "All provider API keys and other backend settings were preserved."
Write-Host ""
Write-Host "Restart the backend and frontend. Task endpoints will use local single-user mode."
