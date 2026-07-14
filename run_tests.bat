@echo off
cd /d "%~dp0"
echo Running MasonMart regression tests...
py -m unittest discover -s tests -v
pause
