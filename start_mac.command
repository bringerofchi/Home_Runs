#!/bin/bash
cd "$(dirname "$0")"
echo "Installing/checking dependencies..."
python3 -m pip install -r requirements.txt -q
echo "Starting MLB HR Edge Lab dashboard..."
python3 webapp/app.py
