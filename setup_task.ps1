# Run this script as Administrator:
#   Right-click setup_task.ps1 → "Run with PowerShell" (then accept UAC prompt)
# Or from an elevated terminal:
#   powershell -ExecutionPolicy Bypass -File "D:\LocalGIT\Delta.Exchange\setup_task.ps1"

$taskName = "DeltaExchange_BTC_Bot"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument '/c "D:\LocalGIT\Delta.Exchange\run_bot.bat"' `
    -WorkingDirectory "D:\LocalGIT\Delta.Exchange"

# At startup + 30 s delay for network
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT30S"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Delta Exchange BTC Options Bot — RSI+Supertrend on BTCUSD"

if ($?) {
    Write-Host "`nTask '$taskName' registered successfully." -ForegroundColor Green
    Write-Host "It will start automatically 30 seconds after the next reboot."
    Write-Host "To start it now run:  Start-ScheduledTask -TaskName '$taskName'"
} else {
    Write-Host "`nRegistration failed. Make sure you are running as Administrator." -ForegroundColor Red
}

Read-Host "`nPress Enter to close"
