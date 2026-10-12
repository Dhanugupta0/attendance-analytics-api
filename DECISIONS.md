# Architectural Decisions

1. **Indexes.**
   - `employees`: unique `emp_code` ensures primary employee lookups and prevent duplicates; compound `(department, emp_code)` covers department filtering with sorted pagination; `joined_on` serves monthly headcount scans.
   - `attendance_logs`: unique compound `(emp_code, date)` prevents multiple daily punch-ins; compound `(date: -1, emp_code: 1)` powers sorted list queries; compound `(emp_code: 1, punch_in: -1)` quickly retrieves the latest punch-in record during punch-out.
   - *Rejected:* A separate index on `status`, because low cardinality (5 values) makes index selectivity poor, and date ranges already narrow the result set.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `insert_one`. The unique index on `(emp_code, date)` allows exactly one insert to commit. The second insert raises `DuplicateKeyError`, which my code catches to return a 409 conflict. No check-then-insert exists.

3. **Ties.**
   The leaderboard pipeline uses `$rank` (standard dense competition ranking 1, 2, 2, 4) and filters `rank <= limit`. Any ties spanning the cutoff boundary are all returned, potentially returning more than `limit` rows. Results sort by late minutes descending, then `emp_code` ascending.

4. **Headcount.**
   The department summary aggregation starts on `employees` (matching `joined_on <= last_day`) and performs a `$lookup` to `attendance_logs`. Headcount is the count of employee documents, ensuring employees with zero attendance records are fully counted.

5. **Scaling to 100x data.**
   I would denormalize `department` directly into `attendance_logs` or maintain precomputed daily rollups to avoid large `$in` arrays and `$lookup` joins, and replace `skip`/`limit` with keyset (cursor) pagination.
