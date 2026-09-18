@echo off
REM Double-click to launch bendwright. It opens your browser; click Open to pick a diagram.
REM You can also drag a *.workflow.json file onto this .bat to open it directly.
cd /d "%~dp0"
python bendwright.py %*
