# Design Decisions

## 1. Indexes

I created unique `emp_code` (duplicate 409) and unique `(emp_code, date)` (punch-in race). `(date desc, emp_code asc)` serves list sorting and date ranges. `(emp_code, punch_in desc)` serves punch-out. `(department, joined_on)` plus `joined_on` serve headcount. I rejected a `status`-only index: five values rarely select, and filters always include date or employee.

## 2. Punch-in race

Both requests compute the same truncated instant, IST date, and late minutes, then both attempt `insert_one`. The unique index lets one succeed (201); the loser raises `DuplicateKeyError`, which I map to 409. I never trust check-then-insert.

## 3. Ties at the cutoff

I rank with `$rank` (competition ranking: 1, 1, 3), sort by total descending and `emp_code` ascending, then keep every row with `rank <= limit`. A tie at the cutoff is fully included, so the response can exceed `limit`.

## 4. Headcount with zero logs

My summary aggregates from `employees` (`joined_on <= month-end`), joins logs with `preserveNullAndEmptyArrays`, and counts distinct employee codes. People with zero logs still count, and late joiners do not.

## 5. Serving 100x the data

I would pre-aggregate daily and monthly results nightly, shard `attendance_logs` by employee, and cache trend and summary results.
