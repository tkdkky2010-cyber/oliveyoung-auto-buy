@echo off
rem Windows 실행: 처음이면 설치부터, 서버는 백그라운드로 띄우고 브라우저를 연다
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" call install.bat
if not exist "models\recognition\korean_PP-OCRv5_mobile_rec\inference.onnx" call install.bat
".venv\Scripts\python.exe" start.py --background %*
