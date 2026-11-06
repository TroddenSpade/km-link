<#
    install-autostart.ps1

    Registers a Task Scheduler entry that starts the Alt+S switcher at logon
    with highest privileges, so you never see a UAC prompt.

        .\install-autostart.ps1                    register + start it now
        .\install-autostart.ps1 -Role Secondary    ...this is NOT the main PC
        .\install-autostart.ps1 -NoAuto            do not check the other PC
        .\install-autostart.ps1 -Status            is it registered / running?
        .\install-autostart.ps1 -Log               show the last 40 log lines
        .\install-autostart.ps1 -Remove            unregister it (see also
                                                   .\uninstall-autostart.ps1)

    It asks one question - is this the machine whose keyboard/mouse you want to
    use when *both* machines are on? - and writes the answer to kmlink.conf.

    The task runs pythonw.exe (no interpreter window at all) and writes its
    output to %LOCALAPPDATA%\kmlink\kmlink.log, so nothing appears on screen:
    use -Log or -Status to look at it.

    Install it on BOTH machines: with the default `auto = yes` they tell each
    other that they are alive (one tiny heartbeat packet every 2 s), so `run`
    can switch only to a machine that is actually on.  Use -NoAuto if the other
    machine will not run this script.

    It elevates itself when needed.  Run it once per Windows install
    (the desktop PC and the laptop's Windows side).
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$Status,
    [switch]$Log,
    [switch]$NoStart,
    [switch]$NoAuto,
    [ValidateSet("Main", "Secondary")]
    [string]$Role,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$TaskName = "KM Link Alt+S switcher"
$Dir      = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script   = Join-Path $Dir "km_link_switch.py"
$Conf     = Join-Path $Dir "kmlink.conf"
$LogFile  = Join-Path $env:LOCALAPPDATA "kmlink\kmlink.log"

function Get-Conf {
    # read one setting back (used by -Status and by the prompt's default)
    param([string]$Key, [string]$Default = "")
    if (-not (Test-Path $Conf)) { return $Default }
    foreach ($line in Get-Content $Conf) {
        if ($line -match "^\s*$Key\s*=\s*(\S+)") { return $Matches[1].ToLower() }
    }
    return $Default
}

function Get-Role { Get-Conf -Key "role" -Default "main" }

function Resolve-Python {
    # `python` on PATH is often the Microsoft Store alias, which prints a
    # "not found" message instead of running anything (and does so silently to
    # the Task Scheduler).  So ask every candidate what its real interpreter is
    # and keep the answers; a broken candidate answers nothing.
    param([string]$Explicit)
    $cands = @()
    if ($Explicit) { $cands += $Explicit }
    foreach ($name in @("python", "python3")) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c -and $c.Source) { $cands += $c.Source }
    }
    $cands += @(
        (Join-Path $env:LOCALAPPDATA "Python\pythoncore-*\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python3*\python.exe"),
        "C:\Python3*\python.exe",
        (Join-Path $env:ProgramFiles "Python3*\python.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Python3*\python.exe")
    )
    $found = @()
    foreach ($c in $cands) {
        if (-not $c) { continue }
        $hit = $null
        if (Test-Path $c) { $hit = $c }
        elseif ($c -match '\*') {
            $hit = (Get-ChildItem -Path $c -ErrorAction SilentlyContinue |
                    Sort-Object FullName -Descending |
                    Select-Object -First 1).FullName
        }
        if (-not $hit) { continue }
        try {
            $real = (& $hit -c "import sys; print(sys.executable)" 2>$null).Trim()
        } catch { continue }
        if ($real -and (Test-Path $real)) { $found += $real }
    }
    # A real install beats the Store shim, which works interactively but not
    # from the Task Scheduler.
    foreach ($f in $found) { if ($f -notlike "*\WindowsApps\*") { return $f } }
    if ($found.Count -gt 0) { return $found[0] }
    return $null
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($Status) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "Not registered."; exit 1 }
    $t | Select-Object TaskName, State | Format-List
    Get-ScheduledTaskInfo -TaskName $TaskName |
        Select-Object LastRunTime, LastTaskResult, NextRunTime | Format-List
    $r = Get-Role
    $a = Get-Conf -Key "auto" -Default "yes"
    Write-Host "role        : $r  ($Conf)"
    if ($r -eq "secondary") {
        Write-Host "              this machine forwards the keyboard/mouse to the other one at start-up"
    } else {
        Write-Host "              this machine owns the keyboard/mouse at start-up"
    }
    Write-Host "other PC    : auto = $a  $(if ($a -eq 'yes') { '(switching needs its heartbeat)' } else { '(never checked)' })"
    $p = @(Get-Process python, pythonw -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -like "*km_link_switch*" })
    if ($p.Count -gt 0) {
        Write-Host "A switcher process is running (PID $($p.Id -join ', '))."
    } else {
        # A task started with highest privileges hides its command line from a
        # non-elevated shell, so fall back to "any windowless python is running".
        $w = @(Get-Process pythonw -ErrorAction SilentlyContinue)
        if ($w.Count -gt 0) {
            Write-Host "Looks like it is running (windowless python, PID $($w.Id -join ', '))"
            Write-Host "  - a command line cannot be read from here; the State above is"
            Write-Host "    the authoritative answer."
        } else {
            Write-Host "No switcher process is running right now."
        }
    }
    $logState = if (Test-Path $LogFile) { "(present)" } else { "(missing)" }
    Write-Host "log file    : $LogFile $logState"
    exit 0
}

if ($Log) {
    if (-not (Test-Path $LogFile)) {
        Write-Host "No log yet at $LogFile"
        Write-Host "(it appears the first time the task starts)."
        exit 1
    }
    Write-Host "log file : $LogFile"
    Write-Host ""
    Get-Content -Path $LogFile -Tail 40
    exit 0
}

if (-not ($Remove -or $Status)) {
    # The one question: which machine should own the keyboard/mouse when both
    # are on?  (If only one is on, the script routes to that one by itself.)
    if (-not $Role) {
        $now = Get-Role
        Write-Host ""
        Write-Host "Is THIS computer the main system?"
        Write-Host "  main       - the keyboard + mouse are plugged in here, and they stay here"
        Write-Host "  secondary  - you normally drive the OTHER machine from here, so this one"
        Write-Host "               forwards the keyboard/mouse to it at start-up"
        $answer = ""
        try { $answer = Read-Host "main or secondary? (default $now)" } catch { }
        switch -Regex ($answer) {
            '^\s*s(econdary)?\s*$' { $Role = "Secondary" }
            '^\s*m(ain)?\s*$'      { $Role = "Main" }
            '^\s*$'                { $Role = $now.Substring(0,1).ToUpper() + $now.Substring(1) }
            default {
                Write-Host "[!] '$answer' is not main or secondary - using '$now'."
                $Role = $now.Substring(0,1).ToUpper() + $now.Substring(1)
            }
        }
    }
}

if (-not (Test-Admin)) {
    Write-Host "Requesting Administrator rights..."
    $arg = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             "`"$($MyInvocation.MyCommand.Path)`"")
    if ($Remove) { $arg += "-Remove" }
    if ($NoStart) { $arg += "-NoStart" }
    if ($NoAuto) { $arg += "-NoAuto" }
    if ($Role) { $arg += @("-Role", $Role) }
    if ($PythonPath) { $arg += @("-PythonPath", "`"$PythonPath`"") }
    Start-Process powershell -Verb RunAs -ArgumentList $arg
    exit 0
}

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'."
    exit 0
}

if (-not (Test-Path $Script)) {
    throw "km_link_switch.py not found next to this script ($Script)"
}

# Resolve the real interpreter: `python` is often the Store alias, which does
# not work reliably from the Task Scheduler.
$Python = Resolve-Python -Explicit $PythonPath
if (-not $Python) {
    throw ("Could not find a working python.exe (the 'python' on PATH is " +
           "probably the Microsoft Store alias).  Pass the real one: " +
           ".\install-autostart.ps1 -PythonPath C:\Path\to\python.exe")
}

# pythonw.exe is the same interpreter with no console window at all, so the
# task never flashes a black window on the desktop.
$Pythonw = $Python
if (Test-Path $Python) {
    $cand = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
    if (Test-Path $cand) { $Pythonw = $cand }
}
if ($Pythonw -ne $Python) {
    Write-Host "Interpreter : $Pythonw  (windowless)"
} else {
    Write-Host "Interpreter : $Python  (pythonw.exe not found - a console window will show)"
}

# The task appends to this file; pythonw has no console, so the log is the only
# place its output goes.  -Log tails it.
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null

# Record the answers, so `run` starts out on the right machine and knows
# whether it is allowed to check the other one.
$Auto = if ($NoAuto) { "no" } else { "yes" }
@(
    "# Written by install-autostart.ps1 - plain text, edit freely.",
    "#",
    "# main      = this machine owns the keyboard/mouse at start-up",
    "# secondary = the other machine is the one you normally drive",
    "role = $($Role.ToLower())",
    "#",
    "# auto = yes: the two machines send each other one tiny heartbeat packet",
    "# every hb_interval seconds, so run knows whether the other machine is on.",
    "# Alt+S is then only followed while it answers, and if it disappears in mid-",
    "# session the keyboard/mouse come back to this machine instead of pointing",
    "# at one that is off.  This needs the script running on the OTHER machine",
    "# too: set auto = no if it is not (then nothing is ever checked).",
    "auto = $Auto",
    "peer_timeout = 5",
    "hb_interval = 2"
) | Set-Content -Path $Conf -Encoding Ascii
Write-Host "Role        : $($Role.ToLower())  ($Conf)"
Write-Host "Other PC    : $(if ($Auto -eq 'yes') { 'auto = yes - checked by heartbeat (needs the script there too)' } else { 'auto = no - never checked' })"

$user      = "$env:USERDOMAIN\$env:USERNAME"
$action    = New-ScheduledTaskAction -Execute $Pythonw `
                 -Argument "`"$Script`" run --log `"$LogFile`"" -WorkingDirectory $Dir
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive `
                 -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                 -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
                 -MultipleInstances IgnoreNew -RestartCount 999 `
                 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description "Reproduces Mac KM Link's Alt+S switching over the OTi USB cable (script, no vendor app, no LAN)." | Out-Null

Write-Host "Registered '$TaskName' (at logon, highest privileges, no window)."
Write-Host "Log file    : $LogFile"
if (-not $NoStart) {
    # Re-registering does not restart a running task (MultipleInstances is
    # IgnoreNew), so stop the old copy first - it may be running older code.
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started it now - it runs in the background; nothing will appear on"
    Write-Host "screen.  Give it a second, then look at the log:"
    Write-Host "  .\install-autostart.ps1 -Log"
}
Write-Host ""
Write-Host "  Install it on the OTHER machine as well: with auto = yes, each machine"
Write-Host "  only forwards to the other one while that one answers its heartbeat."
Write-Host ""
Write-Host "  check : .\install-autostart.ps1 -Status"
Write-Host "  remove: .\install-autostart.ps1 -Remove"
