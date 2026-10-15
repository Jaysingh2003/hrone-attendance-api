# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Used naive local server time and did not enforce the strict grace boundary correctly for all inputs. | A 09:40:01 punch on a 09:30 shift should report 10 late minutes. | Centralized UTC epoch conversion and IST shift calculations. |
| 2 | `compute_work_hours` | Float rounding uses Python's binary/banker's rounding instead of decimal half-up. | A duration whose third decimal is exactly 5 can round down unexpectedly. | Use `Decimal` with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Did not model overnight shift ends on the following calendar day. | A 22:00-06:00 shift calculates morning overtime against the wrong date. | Resolve the shift end date from the employee's shift. |
| 4 | `create_employee` | Duplicate protection was a check-then-insert race, and `created_at` was returned as a datetime. | Concurrent creates can both pass the check; JSON does not match the epoch contract. | Add a unique index, catch `DuplicateKeyError`, and serialize instants. |
| 5 | `list_employees` | `skip` used `page * page_size`, totals ignored the department filter, and results were unsorted. | Page 1 starts at item 21 and filtered totals are too large. | Use `(page - 1) * page_size`, filtered counts, and indexed sorting. |
| 6 | `punch_in` | Unknown employees caused a server error; timestamps were seconds-sensitive and duplicate inserts were non-atomic. | Punching an unknown code or sending concurrent requests produces the wrong status. | Validate the employee, truncate to seconds, and rely on the compound unique index. |
| 7 | `list_attendance` | Loaded the whole collection, omitted stable tie sorting, and converted Mongo IDs into an API field. | Performance degrades at 100k records and response shape exposes internal IDs. | Use database sorting, counting, skip/limit, and omit `_id`. |
| 8 | Missing contract endpoints | Punch-out, corrections, analytics, explain, and startup indexes were absent. | Requests to the documented paths return 404. | Implemented all documented paths and their business rules. |

I did not treat the use of `MongoClient` at module import as a defect: PyMongo connects lazily, which keeps importing the ASGI app fast. Readiness is instead checked explicitly by `/health`.

Also note anything you looked at and decided was **not** a defect, and why.
