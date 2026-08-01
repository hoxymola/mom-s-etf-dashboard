@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ETF 대시보드를 준비합니다...
pip install -q flask finance-datareader
python app.py
pause
