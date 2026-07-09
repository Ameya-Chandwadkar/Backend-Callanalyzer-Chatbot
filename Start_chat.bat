@echo off
cd /d "%~dp0"
echo Starting MasonMart Data Assistant...
echo A browser window will open shortly. Leave THIS window open while you use it.
echo Close this window when you're done to stop the assistant.
echo.
py chat_web.py
pause