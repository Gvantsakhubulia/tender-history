@echo off
cd /d "C:\Users\gvantsa.khubulia\tender-history"
call venv\Scripts\activate.bat
python -u rescraper.py >> logs\rescraper.log 2>&1
