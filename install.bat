@echo off
rem Windows 설치: 가상환경 + 라이브러리 + OCR 모델 + 검증
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
py -3.11 -c "" >nul 2>nul
if %errorlevel%==0 (
  py -3.11 install.py %*
) else (
  py -3 install.py %* || python install.py %*
)
if errorlevel 1 pause
