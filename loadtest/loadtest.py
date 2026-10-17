"""10,000 students stampede 500 seats.  python loadtest/loadtest.py [--base URL] [--users N] [--concurrency N] [--hold SECONDS]"""
import argparse, asyncio, collections, random, statistics, time, uuid
import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000"); ap.add_argument("--users", type=int, default=10000)
ap.add_argument("--concurrency", type=int, default=500); ap.add_argument("--hold", type=int, default=10, help="server HOLD_SECONDS")
A = ap.parse_args()
codes, lat = collections.Counter(), []
run = uuid.uuid4().hex[:6]


async def call(c, method, url, **kw):
    t = time.perf_counter()
    for attempt in range(3):                                 # retry dropped keep-alive sockets (safe: calls are idempotent or 409-guarded)
        try: r = await c.request(method, url, **kw); break
        except httpx.TransportError:
            if attempt == 2: raise
    lat.append((url.split("/")[1], time.perf_counter() - t)); codes[(url.split("/")[1], r.status_code)] += 1
    return r


async def student(c, sem, i, fate):
    async with sem:
        uid = (await call(c, "POST", "/users", json={"name": f"s{i}", "email": f"s{i}-{run}@vit.ac.in"})).json()["id"]
        for _ in range(5):                                   # try up to 5 random seats
            body, hdr = {"user_id": uid, "seat_id": random.randint(1, 500)}, {"Idempotency-Key": str(uuid.uuid4())}
            n = 2 if i % 20 == 0 else 1                           # 5% of users double-click (same Idempotency-Key)
            r = (await asyncio.gather(*[call(c, "POST", "/reservations", json=body, headers=hdr) for _ in range(n)]))[0]
            if r.status_code == 201:
                break
        else:
            return
        bid = r.json()["id"]
        if fate == "abandon": return
        if fate == "retry": await call(c, "POST", f"/bookings/{bid}/pay", json={"mode": "fail"})
        mode = {"delayed": {"mode": "delayed", "delay_seconds": 2}}.get(fate, {"mode": "success"})
        await call(c, "POST", f"/bookings/{bid}/pay", json=mode, headers={"Idempotency-Key": str(uuid.uuid4())})


async def main():
    sem = asyncio.Semaphore(A.concurrency)
    fates = random.choices(["pay", "retry", "delayed", "abandon"], [60, 15, 10, 15], k=A.users)
    async with httpx.AsyncClient(base_url=A.base, timeout=60, limits=httpx.Limits(max_connections=A.concurrency)) as c:
        t0 = time.perf_counter()
        await asyncio.gather(*[student(c, sem, i, f) for i, f in enumerate(fates)])
        dur = time.perf_counter() - t0
        await asyncio.sleep(A.hold + 4)                       # let holds expire + delayed payments settle
        s = (await c.get("/stats")).json()
    print(f"\n== {A.users} users, concurrency {A.concurrency}, {dur:.1f}s, {len(lat)/dur:.0f} req/s ==")
    for ep in ("users", "reservations", "bookings"):
        v = sorted(x for e, x in lat if e == ep)
        if v: print(f"{ep:13s} n={len(v):6d}  p50={statistics.median(v)*1000:6.0f}ms  p95={v[int(len(v)*.95)]*1000:6.0f}ms  p99={v[int(len(v)*.99)]*1000:6.0f}ms")
    print("status codes:", dict(sorted(codes.items())))
    print("final stats :", s)
    assert s["double_booked_seats"] == 0 and s["booked"] <= s["total_seats"], "INVARIANT VIOLATED"
    print("INVARIANT OK: no seat booked twice, booked <= capacity")

asyncio.run(main())
