# start_bots.ps1 — idempotent starter for Mathi's trading stack.
# Starts Delta_Straddle_Live.py and dashboard.py only if not already running.
# Used by Task Scheduler at logon and as a 10-minute watchdog.

$dir = "D:\AI\Delta.Exchange.Mathi"
$py  = "C:\Program Files\Python314\python.exe"

$procs = Get-WmiObject Win32_Process -Filter "name='python.exe'" |
         Select-Object -ExpandProperty CommandLine

$botRunning  = $procs | Where-Object { $_ -like "*Delta_Straddle_Live*" }
$dashRunning = $procs | Where-Object { $_ -like "*dashboard.py*" -and $_ -like "*Delta.Exchange.Mathi*" }

$log = Join-Path $dir "logs\watchdog_starts.log"
$ts  = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

if (-not $botRunning) {
  Start-Process $py -ArgumentList "Delta_Straddle_Live.py" -WorkingDirectory $dir -WindowStyle Hidden
  Add-Content $log "$ts  started Delta_Straddle_Live.py"
}
if (-not $dashRunning) {
  Start-Process $py -ArgumentList "$dir\dashboard.py" -WorkingDirectory $dir -WindowStyle Hidden
  Add-Content $log "$ts  started dashboard.py"
}
