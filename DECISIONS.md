# DECISIONS.md

1. **Indexes.** `employees.emp_code` unique: race-safe employee creation. `employees (department, joined_on)` and `joined_on`: headcount (R9) for summary and trend. `attendance_logs (emp_code, date)` unique: one record per day, which makes punch-in race-safe, and it serves per-employee queries. `attendance_logs (date desc, emp_code asc)`: the `GET /attendance` sort and every date-range scan. Rejected: a separate `date`-only index, redundant with the compound one and extra write cost.

2. **Punch-in race.** Both requests validate, find the employee, and call `insert_one` with no existence check. The unique `(emp_code, date)` index lets exactly one insert commit; the other raises `DuplicateKeyError`, which I return as 409.

3. **Ties.** Ranking is done in the pipeline with `$setWindowFields`/`$rank`, then `rank <= limit` is applied. A tie at the cutoff returns every tied employee, so the response can have more than `limit` rows (ranks 1, 2, 2, 4).

4. **Headcount.** The summary starts from `employees` (`joined_on <= month end`) and `$lookup`s logs with `preserveNullAndEmptyArrays`, so an employee with no logs still reaches the `$group`, where headcount counts distinct `emp_code`s.

5. **100x data.** The trend pipeline joins every log to its employee, and looks up headcount once per day. I would store `department` on each log or keep daily roll-ups, and use keyset instead of skip pagination.