<#
    uninstall-autostart.ps1 - stop and remove the KM Link Alt+S switcher.

        .\uninstall-autostart.ps1              stop it + unregister the task
        .\uninstall-autostart.ps1 -Stop        just stop it (it is back at logon)
        .\uninstall-autostart.ps1 -Purge       ...and delete the log file too
        .\uninstall-autostart.ps1 -DryRun      say what it would do, change nothing
        .\uninstall-autostart.ps1 -Status      what is there right now?

    It removes exactly what install-autostart.ps1 created:

      * the scheduled task "KM Link Alt+S switcher" (stopped first, then
        unregistered), plus any copy of the switcher started by hand
      * kmlink.conf, next to these scripts

    and, only with -Purge, the log folder %LOCALAPPDATA%\kmlink\ .

    The scripts themselves are never touched, and neither is anything else on
    the machine: no service, no driver, no registry key.  Stopping it is enough
    to get ordinary Windows input back - while it runs as `role = main` it never
    takes the keyboard or mouse away.

    Unregistering a task needs Administrator rights, so this elevates itself
    once, the same way the installer does.  (Plain -Stop and -Status do not.)

    Run it on every machine you installed on (the PC, the laptop's Windows side).
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Purge,
    [switch]$DryRun,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$TaskName = "KM Link Alt+S switcher"
$Dir      = Split-Path -Parent $MyInvocation.MyCommand.Path
$Conf     = Join-Path $Dir "kmlink.conf"
$LogDir   = Join-Path $env:LOCALAPPDATA "kmlink"
$LogFile  = Join-Path $LogDir "kmlink.log"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-KmlinkTask {
    # the scheduled task, or $null.  (Subdirectories are not used, so no -Path.)
    param([string]$Name = $TaskName)
    Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
}

function Get-KmlinkProcess {
    # copies of the switcher running outside the task (started by hand).
    # An elevated process hides its command line from a non-elevated shell, so
    # this is only exact once we are elevated - which is when it matters.
    @(Get-Process python, pythonw -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -like "*km_link_switch*" })
}

function Stop-KmlinkTask {
    param([string]$Name = $TaskName)
    if (-not (Get-KmlinkTask -Name $Name)) { return $false }
    Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 20; $i++) {                 # it goes away quickly, but
        if ((Get-KmlinkTask -Name $Name).State -ne "Running") { break }
        Start-Sleep -Milliseconds 250               # do not race the scheduler
    }
    return ((Get-KmlinkTask -Name $Name).State -ne "Running")
}

function Remove-KmlinkTask {
    param([string]$Name = $TaskName)
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    return (-not (Get-KmlinkTask -Name $Name))
}

function Stop-KmlinkProcess {
    $killed = @()
    foreach ($proc in Get-KmlinkProcess) {
        try {
            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
            $killed += $proc.Id
        } catch { }
    }
    return $killed
}

# ------------------------------------------------------------------ -Status
$task  = Get-KmlinkTask
$procs = Get-KmlinkProcess

if ($Status) {
    if ($task) {
        $r = "main"
        if (Test-Path $Conf) {
            foreach ($line in Get-Content $Conf) {
                if ($line -match '^\s*role\s*=\s*(\S+)') { $r = $Matches[1].ToLower() }
            }
        }
        Write-Host "task       : $TaskName  ($($task.State))"
        Write-Host "role       : $r  ($Conf)"
    } else {
        Write-Host "task       : not registered."
    }
    if ($procs.Count -gt 0) {
        Write-Host "process    : running, PID $($procs.Id -join ', ')"
    } else {
        # The task runs elevated, and an elevated process hides its command line
        # from a non-elevated shell, so all we can honestly say is "a windowless
        # python is up" - the task State above is the authoritative answer.
        $w = @(Get-Process pythonw -ErrorAction SilentlyContinue)
        if ($w.Count -gt 0) {
            Write-Host "process    : windowless python running, PID $($w.Id -join ', ')"
            Write-Host "             (command line not readable from here; trust the State)"
        } else {
            Write-Host "process    : none"
        }
    }
    Write-Host "config     : $Conf              $(if (Test-Path $Conf) { '(present)' } else { '(gone)' })"
    Write-Host "log folder : $LogDir  $(if (Test-Path $LogDir) { '(present)' } else { '(gone)' })"
    exit 0
}

# ------------------------------------------------------------------ -DryRun
if ($DryRun) {
    Write-Host "This is a preview; nothing will be changed."
    Write-Host ""
    if ($task) {
        Write-Host "would stop    : task '$TaskName' (now: $($task.State))"
        if ($Stop) {
            Write-Host "would keep    : the task registration (it comes back at logon)"
        } else {
            Write-Host "would remove  : the task registration"
        }
    } else {
        Write-Host "would stop    : nothing - '$TaskName' is not registered"
    }
    if (-not $Stop) {
        if ($procs.Count -gt 0) {
            Write-Host "would remove  : running switcher process(es) PID $($procs.Id -join ', ')"
        }
        if (Test-Path $Conf) { Write-Host "would remove  : $Conf" }
        if ($Purge) {
            if (Test-Path $LogDir) { Write-Host "would remove  : $LogDir (the log folder)" }
        } elseif (Test-Path $LogDir) {
            Write-Host "would keep    : $LogFile   (add -Purge to delete it)"
        }
    }
    Write-Host "would keep    : the scripts in $Dir"
    exit 0
}

# ------------------------------------------------------------------- -Stop
if ($Stop) {
    if ($task) {
        $ok = Stop-KmlinkTask
        if ($ok) { Write-Host "[i] stopped '$TaskName'.  It starts again at your next logon." }
        else     { Write-Host "[!] '$TaskName' is still Running - try again, or use -Status."; exit 1 }
    } else {
        Write-Host "[i] '$TaskName' is not registered, so there is nothing to stop."
    }
    $procs = Get-KmlinkProcess
    if ($procs.Count -gt 0) {
        Write-Host "[!] it is still running outside the task (PID $($procs.Id -join ', '))."
        Write-Host "    Run this again without -Stop (as Administrator) to remove it, or"
        Write-Host "    close the window that started it."
    } else {
        Write-Host "[i] no switcher process is running: the keyboard and mouse are"
        Write-Host "    ordinary Windows input again."
    }
    exit 0
}

# ------------------------------------------------- remove (needs Administrator)
if (-not (Test-Admin)) {
    Write-Host "Requesting Administrator rights..."
    $arg = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             "`"$($MyInvocation.MyCommand.Path)`"")
    if ($Purge) { $arg += "-Purge" }
    Start-Process powershell -Verb RunAs -ArgumentList $arg
    exit 0
}

$done = @()

if ($task) {
    if (-not (Stop-KmlinkTask)) {
        throw "'$TaskName' will not stop; stop it by hand and run this again."
    }
    if (-not (Remove-KmlinkTask)) {
        throw "'$TaskName' could not be unregistered."
    }
    $done += "task '$TaskName' (stopped and unregistered)"
} else {
    Write-Host "[i] '$TaskName' was not registered."
}

$killed = Stop-KmlinkProcess
if ($killed.Count -gt 0) { $done += "switcher process(es) PID $($killed -join ', ')" }

if (Test-Path $Conf) {
    Remove-Item $Conf -Force
    $done += "config $Conf"
}

if ($Purge) {
    if (Test-Path $LogDir) {
        Remove-Item $LogDir -Recurse -Force
        $done += "log folder $LogDir"
    }
}

Write-Host ""
if ($done.Count -gt 0) {
    foreach ($item in $done) { Write-Host "[i] removed: $item" }
} else {
    Write-Host "[i] nothing was installed, so nothing was removed."
}
if (-not $Purge -and (Test-Path $LogFile)) {
    Write-Host "[i] kept     : $LogFile"
    Write-Host "              (run with -Purge to delete the log as well)"
}
Write-Host "[i] kept     : the scripts in $Dir"
Write-Host ""
Write-Host "[i] The keyboard and mouse are ordinary Windows input again, now and"
Write-Host "    after a reboot.  To put it back:  .\install-autostart.ps1"
