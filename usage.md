# 1. Install
pip install -r requirements.txt && playwright install chromium

# 2. Configure
cp .env.example .env       # fill in credentials and login URL
cp targets.json.example targets.json  # fill in course names

# 3. Login (saves persistent session)
python -m automation login

# 4. Run all courses
python -m automation run-all --targets-file targets.json

# 5. Resume after interruption (just re-run)
python -m automation run-all --targets-file targets.json

# 6. Check status
python -m automation status --output-dir course_capture/TR2PRDXA
