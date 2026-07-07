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

$procs = Get-WmiObject Win32_Process -Filter "name='python.exe'" |
         Select-Object -ExpandProperty CommandLine

$botRunning  = $procs | Where-Object { $_ -like "*Delta_Straddle_Live*" }
$dashRunning = $procs | Where-Object { $_ -like "*dashboard.py*" -and $_ -like "*Delta.Exchange.Mathi*" }

$log = Join-Path $dir "logs\watchdog_starts.log"
$ts  = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

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
