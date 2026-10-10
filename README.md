# Employee Attendance and Analytics API

This is my FastAPI plus MongoDB implementation of the full contract. All code is in app main.py.

How to run
1. pip install minus r requirements.txt
2. set MONGO URI and MONGO DB in env. A local env file also works but real env vars win. For local docker I used docker run minus p 27017:27017 mongo 7.
3. python sample seed.py
4. uvicorn app.main:app minus minus port 8000

Health returns 200 only when Mongo pings. Indexes are created at startup.

Notes
Times are epoch millis in the API and dates in Mongo, truncated to seconds, with IST rules R1 to R5.
Analytics for monthly, department summary, late leaderboard and trend are Mongo aggregations. Averages and percents use half up rounding in python.
Nothing is missing. All endpoints from openapi yaml work and I verified them against sample data.
