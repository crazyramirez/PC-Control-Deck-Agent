# Registers / removes the scheduled tasks used by DECKY Agent. Run elevated (by the installer).
#   setup-tasks.ps1 -Exe "C:\Program Files\DECKY Agent\PCControlDeck.exe" -Autostart 1
#   setup-tasks.ps1 -Remove
#
# "DECKY Agent"          on demand, highest privileges: the exe relaunches itself through it, so the
#                            app runs elevated (CPU temperature sensors) without a UAC prompt.
# "DECKY Agent (inicio)" at logon of this user, highest privileges, starts minimized in the tray;
#                            enabled/disabled by the "Start with Windows" switch.
param(
    [string]$Exe = "",
    [int]$Autostart = 1,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$runName = "DECKY Agent"
$logonName = "DECKY Agent (inicio)"
# Earlier names ("PC Control Deck" before 0.10, "Control Deck" until 1.0): their tasks go too.
$legacy = @("PC Control Deck", "PC Control Deck (inicio)", "Control Deck", "Control Deck (inicio)")

foreach ($n in @($runName, $logonName) + $legacy) {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false
    }
}
if ($Remove) { exit 0 }
if (-not (Test-Path $Exe)) { throw "exe not found: $Exe" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
# Laptop-friendly: run on battery, never time out, one instance only.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable

$runAction = New-ScheduledTaskAction -Execute $Exe -WorkingDirectory (Split-Path $Exe)
Register-ScheduledTask -TaskName $runName -Action $runAction -Principal $principal -Settings $settings `
    -Description "DECKY Agent (on demand, elevated for hardware sensors)" | Out-Null

$logonAction = New-ScheduledTaskAction -Execute $Exe -Argument "--minimized" -WorkingDirectory (Split-Path $Exe)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = "PT15S"  # let the network come up
$task = Register-ScheduledTask -TaskName $logonName -Action $logonAction -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Start DECKY Agent with Windows (tray)"
if ($Autostart -eq 0) { Disable-ScheduledTask -TaskName $logonName | Out-Null }
Write-Output "tasks registered for $user (autostart=$Autostart)"
