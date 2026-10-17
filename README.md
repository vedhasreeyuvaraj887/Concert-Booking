# Concert Ticket Booking Backend

500 seats, 10,000 students, zero double-bookings. **FastAPI + PostgreSQL**, Docker, nginx.
Extras implemented: idempotency keys, waiting queue with automatic seat reallocation, reverse proxy.

## Run it in VS Code (5 steps)

Prereqs: Docker Desktop, Python 3.11+, VS Code (extensions: *Python*, *REST Client*, optionally *Markdown Preview Mermaid Support*).

```bash
docker compose up -d db                  # 1. PostgreSQL on localhost:5432
python -m venv .venv                     # 2. virtualenv
source .venv/bin/activate                #    Windows: .venv\Scripts\activate
pip install -r requirements.txt          # 3. dependencies
uvicorn app.main:app --reload            # 4. API on :8000   (or press F5 in VS Code)
```
5. Open **http://localhost:8000/docs** (interactive Swagger) or open `requests.http` and click *Send Request*.

Tables and the 500 seats (A1..Y20) are created automatically on startup.

### Full stack in Docker (db + app + nginx)
```bash
docker compose up --build                # API via proxy: http://localhost:8080/docs
docker compose up --build --scale app=3  # 3 app replicas behind nginx
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/users` | register `{name,email}` (upsert by email) |
| GET | `/seats?status=AVAILABLE\|HELD\|BOOKED` | view seats |
| POST | `/reservations` | hold a seat `{user_id,seat_id}` -> 201, or **409** if taken. Header `Idempotency-Key` |
| GET | `/bookings/{id}` | reservation status + `seconds_left` + payment status |
| POST | `/bookings/{id}/pay` | `{"mode":"success\|fail\|delayed"}` (+`delay_seconds`, `delayed_outcome`). 200 paid, 402 failed, 202 pending, 410 expired. Header `Idempotency-Key` |
| POST | `/bookings/{id}/cancel` | cancel; refunds if paid; seat reallocated |
| GET | `/users/{id}/bookings` | booking history |
| POST / GET | `/waitlist`, `/waitlist/{id}` | join queue when sold out / see position & promotion |
| GET | `/stats`, `/health` | counts + double-booking invariant check |

Booking states: `HELD -> PENDING_PAYMENT -> CONFIRMED`, or `EXPIRED` / `CANCELLED`.

### Example
```bash
curl -X POST localhost:8000/users -H 'Content-Type: application/json' -d '{"name":"Asha","email":"asha@vit.ac.in"}'
curl -X POST localhost:8000/reservations -H 'Content-Type: application/json' -H 'Idempotency-Key: k1' \
     -d '{"user_id":1,"seat_id":7}'
curl -X POST localhost:8000/bookings/1/pay -H 'Content-Type: application/json' -H 'Idempotency-Key: p1' -d '{"mode":"success"}'
curl localhost:8000/users/1/bookings
```

## Configuration (env vars)
`DATABASE_URL`, `HOLD_SECONDS` (reservation lifetime, default 120), `TOTAL_SEATS` (500), `POOL_SIZE` (20 per worker).

## Testing
```bash
HOLD_SECONDS=4 uvicorn app.main:app &     # fresh DB; short hold so expiry is quick to test
python loadtest/smoke.py                  # 15 functional checks (expiry, idempotency, payments, waitlist...)
HOLD_SECONDS=10 uvicorn app.main:app &    # fresh DB
python loadtest/loadtest.py               # 10,000 concurrent users; asserts the no-double-booking invariant
```

## Docs
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): ERD, workflow diagrams, concurrency handling, scaling plan
- [`docs/LOAD_TEST.md`](docs/LOAD_TEST.md): load-test results

## Known limitations
No authentication (the client sends `user_id`); payments are simulated; single event only.
