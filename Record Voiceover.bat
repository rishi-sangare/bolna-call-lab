@echo off
:: Opens the Voice-over Studio on the newest demo recording in this folder.
cd /d "%~dp0"
for /f "delims=" %%d in ('dir /b /ad /o-n recordings ^| findstr /r "^2"') do (
  if exist "recordings\%%d\demo.mp4" ( set "REC=%~dp0recordings\%%d" & goto :found )
)
echo No finished recording found (run: .venv\Scripts\python -m demo.make_video)
pause
exit /b 1
:found
start "" "D:\upwork\Protanki Multi-Account Clicker Tool\.venv\Scripts\pythonw.exe" "D:\upwork\Protanki Multi-Account Clicker Tool\tools\voiceover_studio.py" "%REC%"
