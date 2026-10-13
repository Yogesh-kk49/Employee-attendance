# DECISIONS.md

1. **Indexes.** `employees.emp_code` unique: race-safe employee creation. `employees (department, joined_on)` and `joined_on`: headcount (R9) for summary and trend. `attendance_logs (emp_code, date)` unique: one record per day, making punch-in race-safe and serving employee lookups. `attendance_logs (date desc, emp_code asc)`: powers paginated `GET /attendance`. `attendance_logs (date asc)`: accelerates date-range filtering across analytics aggregation pipelines.

2. **Punch-in race.** Both requests validate, fetch the employee, and execute `insert_one` without pre-checking. The unique `(emp_code, date)` compound index ensures only one insert commits; the concurrent insert raises `DuplicateKeyError`, handled as HTTP 409.

3. **Ties.** Competition ranking uses `$setWindowFields` with `$rank`, filtering by `rank <= limit`. Any ties at the cutoff return all tied employees, allowing responses to exceed `limit` rows (e.g. ranks 1, 2, 2, 4).

4. **Headcount.** Department summary starts from `employees` (`joined_on <= month end`) and `$lookup`s attendance logs with `preserveNullAndEmptyArrays: true`, ensuring employees without logs still contribute to headcount.

5. **100× data.** To scale, I would denormalize `department` onto each attendance log to avoid initial `$lookup` joins, pre-aggregate daily headcount rollups, and switch from offset to keyset pagination.