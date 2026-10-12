# Employee Attendance & Analytics API

Production-ready Employee Attendance & Analytics API built with FastAPI, PyMongo, and MongoDB 7.

## How to Run

1. **Virtual environment and dependencies:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Environment configuration:**
   Set environment variables (or define in a local `.env` file):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Seed sample data (optional):**
   ```bash
   python sample_seed.py
   ```

4. **Start the API server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

Indexes are created automatically at application startup during the lifespan context.

---

## Assumptions & Design Details

1. **Error Check Order:**
   Where API specification edge cases were ambiguous, the endpoint validation order consistently checks:
   - `404 Not Found` (missing employee, missing attendance record), then
   - `409 Conflict` (duplicate entries, already punched in/out, concurrent race updates), then
   - `422 Unprocessable Content` (invalid inputs, timestamps out of range, logic contradictions).

2. **Department Trend Validation Order:**
   For `GET /analytics/departments/{department}/trend`, bad date ranges (invalid date, `to < from`, or `range > 92 days`) return `422` before querying the database, which returns `404` if the department does not exist.

3. **7-Day Trend Moving Average:**
   The moving average window uses the current calendar day plus up to 6 preceding calendar days in the window. Days where the daily rate is `null` (e.g. non-working weekends or zero headcount) stay within the 7-day window, but are excluded from the average calculation. Moving averages use the exact daily rate values.

4. **Seed Document in Trend Pipeline:**
   The trend aggregation pipeline uses MongoDB's `$densify` operator to guarantee a continuous date sequence across all days in `from..to`. In MongoDB, `$densify` requires at least one document in its input stream; if a department has zero attendance logs in the entire query window, `$densify` would otherwise produce zero output documents. To ensure exact completeness, a single seed document dated `from_dt - 1 day` is prepended via `$unionWith` (using an index-covered `$match` on employee), densification runs across the full range, and the seed row is filtered out with `$match: {day: {$gte: from_dt}}`.

5. **Index Hint in Department Summary:**
   The `/analytics/departments/summary` pipeline starts from the `employees` collection to ensure employees with zero attendance records are included in the department headcount. An index hint (`{"joined_on": 1}` when no department filter is specified, or `{"department": 1, "emp_code": 1}` when filtered by department) is passed directly to the aggregation query and explain builder to guarantee index usage (`IXSCAN`) and eliminate any `COLLSCAN`.

6. **Completeness:**
   Nothing is missing. All business rules R1–R10, endpoints, validation constraints, indexes, serializers, and explain capabilities are fully implemented in `app/main.py`.
