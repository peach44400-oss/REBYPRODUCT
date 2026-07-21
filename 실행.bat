@echo off
chcp 65001 >nul
title REBYPRODUCT 재고관리
cd /d "%~dp0"
start "" http://127.0.0.1:8600
python app\main.py
pause
