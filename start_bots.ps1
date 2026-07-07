# start_bots.ps1 — idempotent starter for Mathi's trading stack.
# Starts Delta_Straddle_Live.py and dashboard.py only if not already running.
# Used by Task Scheduler at logon and as a 10-minute watchdog.
#
# IMPORTANT — two Windows gotchas this script works around:
# 1. When this script runs as a Scheduled Task (SYSTEM), Windows puts the
#    whole process tree in a Job Object with kill-on-job-close semantics.
#    Start-Process children get killed the moment this script exits, silently
#    undoing the very thing the watchdog was supposed to guarantee.
#    Fix: launch via Win32_Process.Create (WMI), which runs outside that job.
# 2. Processes created via Win32_Process.Create get no console, so
#    sys.stdout/sys.stderr are None in the child — Python crashes the
#    instant it logs anything (our bot uses StreamHandler(sys.stdout)).
#    Fix: launch through cmd.exe with explicit `>> file 2>&1` redirection,
#    which gives the child real file-backed stdout/stderr handles.

$dir = "D:\AI\Delta.Exchange.Mathi"
$py  = "C:\Program Files\Python314\python.exe"

$log = Join-Path $dir "logs\watchdog_starts.log"
$ts  = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Port 5001 should have exactly one owner. If a stale/duplicate process is
# also bound there (e.g. an old instance that never got replaced when code
# changed), kill every PID on that port and let the checks below relaunch
# a single fresh dashboard.py with current code. Running as SYSTEM here
# means this can clean up processes a normal user session can't touch.
$portOwners = Get-NetTCPConnection -LocalPort 5001 -ErrorAction SilentlyContinue |
              Select-Object -ExpandProperty OwningProcess -Unique
if ($portOwners.Count -gt 1) {
  foreach ($portPid in $portOwners) {
    Stop-Process -Id $portPid -Force -ErrorAction SilentlyContinue
  }
  Add-Content $log "$ts  cleared $($portOwners.Count) conflicting processes on port 5001: $($portOwners -join ',')"
  Start-Sleep -Seconds 1
}

$procs = Get-WmiObject Win32_Process -Filter "name='python.exe'" |
         Select-Object -ExpandProperty CommandLine

$botRunning  = $procs | Where-Object { $_ -like "*Delta_Straddle_Live*" }
$dashRunning = (-not ($portOwners.Count -gt 1)) -and
               ($procs | Where-Object { $_ -like "*dashboard.py*" -and $_ -like "*Delta.Exchange.Mathi*" })

function Start-Detached($exe, $scriptArgs, $workDir, $outFile) {
  $cmdLine = "cmd.exe /c `"`"$exe`" $scriptArgs >> `"$outFile`" 2>&1`""
  $result  = Invoke-WmiMethod -Class Win32_Process -Name Create -ArgumentList @($cmdLine, $workDir, $null)
  return $result.ReturnValue -eq 0
}

if (-not $botRunning) {
  $ok = Start-Detached $py "Delta_Straddle_Live.py" $dir (Join-Path $dir "logs\bot_wmi_stdout.log")
  Add-Content $log "$ts  started Delta_Straddle_Live.py (ok=$ok)"
}
if (-not $dashRunning) {
  $ok = Start-Detached $py "dashboard.py" $dir (Join-Path $dir "logs\dashboard_wmi_stdout.log")
  Add-Content $log "$ts  started dashboard.py (ok=$ok)"
}
