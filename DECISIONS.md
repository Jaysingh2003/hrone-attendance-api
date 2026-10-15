# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index enforces the business identifier. A unique `(emp_code, date)` attendance index enforces one record per shift day. Attendance also has `(date desc, emp_code)` for listing and `(emp_code, date, status)` for employee analytics. I would reject a standalone status index because status is not selective enough for the main queries.
2. **Punch-in race.** Both requests read the employee, then try the same insert. MongoDB's unique `(emp_code, date)` index allows one insert and raises `DuplicateKeyError` for the other, which becomes 409. No check-then-insert decision controls correctness.
3. **Ties.** Rows are sorted by total minutes and code. Ranking uses the row's competition position, so equal totals share a rank and all rows at a rank at or below `limit` are returned, even if that exceeds `limit` rows.
4. **Headcount.** The summary starts from employees joined by the end of the month and groups their attendance logs afterward. Employees with no matching logs remain in that employee-derived department set and still contribute to headcount.
5. **100x data.** I would move trend gap filling and all department rollups into one server-side aggregation, then add explain-driven compound indexes and precomputed monthly summaries if latency still required it.
