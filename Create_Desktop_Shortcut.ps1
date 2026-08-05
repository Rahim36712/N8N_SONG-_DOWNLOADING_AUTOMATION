# PowerShell script to create a Desktop shortcut for Auto Song Downloader
$WshShell = New-Object -ComObject WScript.Shell
$DesktopPath = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Desktop)
$ShortcutPath = Join-Path -Path $DesktopPath -ChildPath "Auto Song Downloader.lnk"

$TargetScript = Join-Path -Path $PSScriptRoot -ChildPath "Start_Silent.vbs"

$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "wscript.exe"
$Shortcut.Arguments = "`"$TargetScript`""
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.Description = "Launch Auto Song Downloader Web App"
# Use shell32.dll music icon (icon index 137 or 268)
$Shortcut.IconLocation = "shell32.dll, 137"
$Shortcut.Save()

Write-Host "Desktop shortcut created successfully at: $ShortcutPath" -ForegroundColor Green
