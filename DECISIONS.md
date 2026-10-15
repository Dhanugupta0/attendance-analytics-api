# Architectural Decisions

1. **Indexes.**
   `employees` has unique `emp_code` (no duplicates), `(department, emp_code)` (sorted department lists), and `joined_on` (headcount). `attendance_logs` has unique `(emp_code, date)` (prevents double punch-in), `(date: -1, emp_code: 1)` (sorted queries), and `(emp_code: 1, punch_in: -1)` (punch-out lookup). I rejected indexing `status` because it has only five values and date filters narrow searches.

2. **Punch-in race.**
   Both requests compute the same date and attempt `insert_one`. The unique `(emp_code, date)` index lets one succeed. The loser raises `DuplicateKeyError`, and my code returns 409 Conflict without any check before inserting.

3. **Ties.**
   The leaderboard uses `$rank`, so ties share a rank and skip subsequent ranks (1, 2, 2, 4). I return all rows where `rank <= limit`. A tie at the cutoff returns more than `limit` rows, sorted by late minutes descending then `emp_code`.

4. **Headcount.**
   The department summary starts from `employees` with `joined_on <= last_day` and looks up logs. Headcount is the count of employee documents, so people with no logs still count.

5. **100x data.**
   I would store `department` on each attendance log or keep daily rollups so the trend avoids long `emp_code` lists, and switch to cursor-based pagination.
