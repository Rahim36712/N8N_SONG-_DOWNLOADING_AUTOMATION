Set WshShell = CreateObject("WScript.Shell")
' Run Start Downloader.bat silently (0 = hidden window)
WshShell.Run "cmd /c ""Start Downloader.bat""", 0, False
