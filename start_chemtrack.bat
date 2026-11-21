@echo off
REM Double-click to start ChemTrack. Close this window (or press Ctrl+C) to stop it.
cd /d "%~dp0"
title ChemTrack
python serve.py
pause
