@echo off
cd /d "%~dp0"
python pdf_rasterizer.py
if errorlevel 1 pause
