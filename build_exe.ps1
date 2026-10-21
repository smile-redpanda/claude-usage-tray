# Builds dist\ClaudeUsageTray.exe: one file with Python and all packages inside, so it runs on any Windows PC.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File build_exe.ps1
$repositoryRoot = $PSScriptRoot
Set-Location $repositoryRoot
& .venv\Scripts\python.exe -m PyInstaller --onefile --windowed --noconfirm `
    --name ClaudeUsageTray `
    --icon "$repositoryRoot\assets\claude_usage_tray.ico" `
    --hidden-import pystray._win32 `
    --distpath dist --workpath build --specpath build `
    tray_app.py
