# Graceful engine shutdown on Windows (ADR 0001).
# WMI-created children receive no console signals, so the engine polls a
# sentinel file every 2 seconds instead.
$dir  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$stop = Join-Path $dir "data\engine.stop"
New-Item -ItemType File -Path $stop -Force | Out-Null
Write-Host "stop sentinel written: $stop (engine exits within ~2s)"
