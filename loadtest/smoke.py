"""End-to-end functional check. Run with HOLD_SECONDS=4 on a fresh DB:  python loadtest/smoke.py [base_url]"""
import sys, time, uuid, httpx

B = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
c = httpx.Client(base_url=B, timeout=30)
tag = uuid.uuid4().hex[:6]
def user(n): return c.post("/users", json={"name": n, "email": f"{n}-{tag}@vit.ac.in"}).json()["id"]
def ok(cond, msg): print(("PASS " if cond else "FAIL ") + msg); assert cond
a, b, w = user("a"), user("b"), user("w")

r1 = c.post("/reservations", json={"user_id": a, "seat_id": 1}, headers={"Idempotency-Key": "k1"})
r2 = c.post("/reservations", json={"user_id": a, "seat_id": 1}, headers={"Idempotency-Key": "k1"})
ok(r1.status_code == 201 and r2.json()["id"] == r1.json()["id"] and r2.headers.get("idempotent-replay"), "idempotent reserve replays same booking")
ok(c.post("/reservations", json={"user_id": b, "seat_id": 1}).status_code == 409, "second user cannot take held seat")
bid = r1.json()["id"]
ok(c.post(f"/bookings/{bid}/pay", json={"mode": "fail"}).status_code == 402, "failed payment -> 402, booking still HELD")
ok(c.get(f"/bookings/{bid}").json()["status"] == "HELD", "status HELD after failed payment")
p1 = c.post(f"/bookings/{bid}/pay", json={"mode": "success"}, headers={"Idempotency-Key": "p1"})
p2 = c.post(f"/bookings/{bid}/pay", json={"mode": "success"}, headers={"Idempotency-Key": "p1"})
ok(p1.json()["status"] == "CONFIRMED" and p2.status_code == 200, "retry pays; repeated pay request is safe")
ok(c.get("/stats").json()["booked"] == 1, "one booked seat")

d = c.post("/reservations", json={"user_id": b, "seat_id": 2}).json()["id"]
ok(c.post(f"/bookings/{d}/pay", json={"mode": "delayed", "delay_seconds": 2}).status_code == 202, "delayed payment accepted (202)")
time.sleep(5.5)  # longer than HOLD: PENDING_PAYMENT must NOT expire
ok(c.get(f"/bookings/{d}").json()["status"] == "CONFIRMED", "delayed payment settled -> CONFIRMED (seat was protected)")

e = c.post("/reservations", json={"user_id": b, "seat_id": 3}).json()["id"]
time.sleep(5.5)
ok(c.get(f"/bookings/{e}").json()["status"] == "EXPIRED", "unpaid hold expires")
ok(c.post("/reservations", json={"user_id": a, "seat_id": 3}).status_code == 201, "expired seat can be re-reserved")
ok(c.post(f"/bookings/{e}/pay", json={}).status_code == 410, "paying an expired booking -> 410")

# fill the hall, join waitlist, cancel -> seat reallocated to waiter
for s in range(4, 501): c.post("/reservations", json={"user_id": a, "seat_id": s})
ok(c.get("/seats", params={"status": "AVAILABLE"}).json() == [], "hall full")
wl = c.post("/waitlist", json={"user_id": w}).json()["waitlist_id"]
ok(c.get(f"/waitlist/{wl}").json()["position"] == 1, "waitlist position 1")
ok(c.post(f"/bookings/{bid}/cancel").json()["status"] == "CANCELLED", "confirmed booking cancelled (refund)")
x = c.get(f"/waitlist/{wl}").json()
ok(x["status"] == "PROMOTED" and c.get(f"/bookings/{x['booking_id']}").json()["status"] == "HELD", "freed seat reallocated to waitlisted user")
print(c.get(f"/users/{a}/bookings").json()[:2]); print(c.get("/stats").json())
