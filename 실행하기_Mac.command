#!/bin/bash
cd "$(dirname "$0")"
echo "ETF 대시보드를 준비합니다..."
pip3 install -q flask finance-datareader
python3 app.py
