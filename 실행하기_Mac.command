#!/bin/bash
cd "$(dirname "$0")"
echo "ETF 대시보드를 준비합니다..."

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt
python app.py
