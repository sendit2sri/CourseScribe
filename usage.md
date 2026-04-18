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


python extract_courses.py catalog.html --pretty --output courses.json

python extract_courses.py catalog.html --category "Core Banking" --pathway "Transact Business Accredited" --make-targets --pretty -o targets.json

python -m automation login

python -m automation run-all --targets-file targets.json --enable-crops

--enable-crops

  - Log output shows filter messages (use --log-level DEBUG)  

python -m automation process --output-dir <path-to-course-output>   

  Usage examples:                                                                 
  # Default: 3s page delay, reading breaks every 15-30 pages (2-5 min each), no   
  batch limit                                                                     
  python -m automation run --start-url URL                                        
                                                                                  
  # Batch of 40 pages, then exit (code 2). Resume picks up from checkpoint.       
  python -m automation run --start-url URL --batch-size 40                        
                                                                                  
  # Custom pacing: breaks every 10-20 pages, 1-3 min each                         
  python -m automation run --start-url URL --idle-pause-interval 10-20 --idle-pause-duration 60-180  

  python -m automation run-all --targets-file targets.json --idle-pause-interval 10-20 --idle-pause-duration 60-180                      
                                                                                  
  # Disable reading breaks entirely                         
  python -m automation run --start-url URL --idle-pause-interval 0 

  Faster but still human-looking:


python -m automation run-all --targets-file targets.json --page-delay 2 --idle-pause-interval 25-40 --idle-pause-duration 30-90

python -m automation run-all --targets-file targets.json --page-delay 2 --idle-pause-interval 25-40 --idle-pause-duration 30-90

This gives:

2s page delay (1.4-3s with jitter) — still realistic
Breaks of 30-90s every 25-40 pages — less frequent, shorter
~5-6s/page effective
That's roughly 3-4x faster. If you have ~500 pages total, that's ~45 min vs ~3 hours.

If you want even faster and are comfortable with the risk:


python -m automation run-all --targets-file targets.json --page-delay 1 --idle-pause-interval 0

  python -m automation run-all --targets-file
  targets_wealth_mgmt.json   