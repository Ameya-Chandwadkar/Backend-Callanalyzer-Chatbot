@echo off
cd /d "%~dp0"
echo [%date% %time%] Running Shopify sync >> shopify_sync.log
"C:\Users\DELL\AppData\Local\Python\pythoncore-3.14-64\python.exe" ingest_shopify.py >> shopify_sync.log 2>&1
echo [%date% %time%] Done, exit code %errorlevel% >> shopify_sync.log
