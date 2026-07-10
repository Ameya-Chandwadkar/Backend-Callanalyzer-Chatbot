@echo off
cd /d "%~dp0"
echo Starting MasonMart auto-ingest watcher...
echo Export CSVs from Callyzer into the "incoming" folder and they'll be
echo ingested automatically. Leave THIS window open while it watches.
echo Close this window (or press Ctrl+C) to stop.
echo.
py watch_incoming.py
pause
