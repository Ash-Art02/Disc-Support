# Run once in PowerShell to auto-start the bot on Windows login (hidden, no window).
# Right-click -> Run with PowerShell, or: powershell -ExecutionPolicy Bypass -File install_autostart.ps1
$BotDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "DiscordSupportBot"
$Action = New-ScheduledTaskAction -Execute "pythonw" -Argument "`"$BotDir\run.py`"" -WorkingDirectory $BotDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Discord support bot - background, auto-start on login" | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Done! Bot now starts automatically on login, no PowerShell window needed."
Write-Output "Manage it with: Task Scheduler -> $TaskName (Stop/Disable to turn off)."
