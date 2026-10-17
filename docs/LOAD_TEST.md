# Load test results

**Scenario** (`loadtest/loadtest.py`): 10,000 students register and stampede 500 seats at once (500 concurrent clients).
Each student tries up to 5 random seats; 5% double-click (duplicate request, same `Idempotency-Key`).
Outcomes after reserving: 60% pay, 15% fail once then pay, 10% delayed payment (2s), 15% abandon. Hold time 10s.

**Environment**: single sandbox VM with **1 vCPU** running PostgreSQL 16, 2 uvicorn workers *and* the load generator
together, so absolute latency/throughput are pessimistic (requests queue behind one core). The point of this run is correctness under contention, not peak throughput.

```
== 10000 users, concurrency 500, 564.8s, 107 req/s ==
users         n= 10000  p50=  3469ms  p95= 13465ms  p99= 20952ms
reservations  n= 49678  p50=  3221ms  p95= 13717ms  p99= 20917ms
bookings      n=   825  p50=  4396ms  p95= 16584ms  p99= 24371ms
status codes: {('bookings', 200): 436, ('bookings', 202): 64, ('bookings', 402): 102, ('bookings', 410): 223, ('reservations', 201): 862, ('reservations', 409): 48816, ('users', 201): 10000}
final stats : {'total_seats': 500, 'booked': 500, 'held': 0, 'waiting': 0, 'double_booked_seats': 0}
INVARIANT OK: no seat booked twice, booked <= capacity
```

## What it shows
- **No double-booking**: `double_booked_seats = 0`, `booked = 500 = capacity`. 48,816 of 49,678 reservation attempts correctly got `409`.
- **Abandonment handled**: 862 successful holds produced 500 sales; the rest expired/abandoned and the seats were re-sold.
- **Payments**: `200` paid, `402` simulated failures (retried), `202` delayed (all settled to CONFIRMED), `410` late payers whose hold had already expired (queueing delay > 10s hold in this constrained run).
- **Zero server errors** (no 5xx).
- Expect an order-of-magnitude better latency on a multi-core host with the DB on its own CPU; scale-out steps are in `ARCHITECTURE.md`.
