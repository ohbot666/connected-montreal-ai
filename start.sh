#!/bin/bash
cd ~/Projects/connected-montreal-ai
python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
pip install -q -r requirements.txt
export $(cat .env | grep -v '^#' | xargs)
echo "✅ Starting at http://localhost:5050"
python3 server.py
