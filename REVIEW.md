# Review

I reviewed the starter app main.py like a pull request. These are the defects I found and fixed.

1. compute late minutes
What was wrong: it checked minutes greater than 10 instead of seconds greater than 600. So a punch at 09:40:01 gave 0 late instead of 10. It also used server local time instead of IST.
How I noticed it: I punched at 09:40:01 IST and expected 10 late but got 0. I also ran with different timezones and got different results.
How I fixed it: I compare seconds after shift start, return 0 if 600 or less else floor minutes. I build shift start in IST and compare in IST.

2. Overnight shift date
What was wrong: no handling for night shifts like 22 to 06. An early morning punch got the wrong attendance date and wrong late.
How I noticed it: I punched at 01:00 in the night with a 22 to 06 shift. It gave date July 7 but I expected July 6.
How I fixed it: I added a helper that takes IST date and goes to previous day when the shift is overnight and punch time is before shift end.

3. Work hours rounding
What was wrong: it used python round which is bankers rounding, not half up.
How I noticed it: I tested 8442 seconds which is 2.345 hours. It gave 2.34 but I expected 2.35.
How I fixed it: I used Decimal with round half up.

4. Overtime
What was wrong: it had no 30 minute minimum, mixed naive and aware datetimes, and never added one day for overnight shifts.
How I noticed it: I punched out 5 minutes after shift end and got 5 overtime but I expected 0. Night shift overtime was completely wrong.
How I fixed it: I build shift end in IST with plus one day for overnight, and I return 0 if less than 1800 seconds else floor minutes.

5. Health check
What was wrong: it always returned ok without checking Mongo.
How I noticed it: I stopped Mongo and it still returned 200 but I expected 503.
How I fixed it: I ping Mongo and raise 503 on failure.

6. Create employee race
What was wrong: it did find one then insert with no unique index, so parallel requests could both succeed.
How I noticed it: I sent 5 parallel posts with same emp code and got more than one 201.
How I fixed it: I added a unique index on emp code and I catch duplicate key as 409. I did the same for punch in.

7. Create employee fields
What was wrong: created at used local naive time, returned ISO string not millis, and had no validation for emp code email shift or joined on.
How I noticed it: I posted a bad email and same shift start and end and both were accepted. Created at was a string not a number.
How I fixed it: I use UTC truncated time, I return millis, and I added strict validation with 422 on bad input.

8. List employees
What was wrong: skip was page times page size so page 1 skipped 20, total ignored department filter, no sorting and no validation.
How I noticed it: I asked for page 1 and missed the first 20. Filtered total was wrong and order was random.
How I fixed it: I use page minus 1 times page size, I count with filter, I sort by emp code, and I validate page and page size.

9. Punch in
What was wrong: unknown employee gave 500 not 404, race allowed double punch, date used server local not IST, no truncation, accepted ABSENT and seconds or float timestamps, and returned id and datetimes.
How I noticed it: I tried unknown emp and got 500. I tried double punch and got two 201s. ABSENT and small timestamps were accepted.
How I fixed it: I return 404 for unknown emp, I rely on unique emp code plus date for 409, I compute IST date with truncation, I allow only PRESENT WFH ON DUTY with strict millis, and I return millis with no id.

10. List attendance
What was wrong: it loaded everything into memory and sorted in python by date only, returned id and datetimes, and had no validation for date range or pagination.
How I noticed it: I saw it would be slow near 100k docs, same date rows were unordered, and date from greater than date to returned 200.
How I fixed it: I use DB sort skip limit with date desc emp code asc, I count in DB, I add 422 checks, and I return millis with an index.

11. Request models
What was wrong: no patterns or limits, punched at accepted floats strings and seconds values.
How I noticed it: I sent a float timestamp and it passed but I expected 422.
How I fixed it: I use strict int with min and max, regex for emp code email and shift, and I ignore extra derived fields.

12. Missing indexes
What was wrong: only default id index, so everything was a collection scan and races had no guarantee.
How I noticed it: I checked explain and saw COLLSCAN.
How I fixed it: I create indexes at startup for emp code unique, emp code plus date unique, date plus emp code, emp code plus punch in, status, department plus joined on, and joined on.

Things I checked and left as is:
IST constant is correct, IST has no daylight saving.
load dotenv without override is correct, real env vars already win.
Status default PRESENT is correct, only the missing allow list was the bug.
