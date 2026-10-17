import asyncio, os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, EmailStr, Field

DSN = os.getenv("DATABASE_URL", "postgresql://ticket:ticket@localhost:5432/ticketing")
HOLD = int(os.getenv("HOLD_SECONDS", "120"))
SEATS = int(os.getenv("TOTAL_SEATS", "500"))
POOL = int(os.getenv("POOL_SIZE", "20"))
LIVE = "('HELD','PENDING_PAYMENT','CONFIRMED')"
# status shown to clients: a HELD booking past its deadline is EXPIRED even before the sweeper runs
EFF = "CASE WHEN b.status='HELD' AND b.expires_at<=now() THEN 'EXPIRED' ELSE b.status END"

pool = AsyncConnectionPool(DSN, min_size=2, max_size=POOL, open=False, kwargs={"row_factory": dict_row})


async def one(conn, sql, *args):
    return await (await conn.execute(sql, args)).fetchone()


async def many(conn, sql, *args):
    return await (await conn.execute(sql, args)).fetchall()


# ---------- helpers (always called inside a transaction) ----------
async def idem_begin(conn, scope, key):
    """None -> we own the key, proceed. Otherwise a replay response of the first call."""
    if not key:
        return None
    if await one(conn, "INSERT INTO idempotency_keys(scope,key) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING key", scope, key):
        return None  # a concurrent duplicate blocks on this insert until we commit, then replays
    r = await one(conn, "SELECT status_code, response FROM idempotency_keys WHERE scope=%s AND key=%s", scope, key)
    if r["status_code"] is None:  # defensive: original request never recorded a result
        return JSONResponse({"detail": "Original request has no recorded result; retry with a new key"}, 409)
    return JSONResponse(r["response"], r["status_code"], headers={"Idempotent-Replay": "true"})


async def idem_end(conn, scope, key, code, body):
    body = jsonable_encoder(body)
    if key:
        await conn.execute("UPDATE idempotency_keys SET status_code=%s, response=%s WHERE scope=%s AND key=%s",
                           (code, Jsonb(body), scope, key))
    return JSONResponse(body, code)


async def expire_stale(conn, seat_id=None, limit=500):
    """HELD past deadline -> EXPIRED. SKIP LOCKED keeps concurrent sweepers/requests from blocking each other."""
    rows = await many(conn, """
        WITH s AS (SELECT id FROM bookings WHERE status='HELD' AND expires_at<=now()
                   AND (%s::int IS NULL OR seat_id=%s) ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED)
        UPDATE bookings b SET status='EXPIRED', updated_at=now() FROM s WHERE b.id=s.id RETURNING b.seat_id""",
        seat_id, seat_id, limit)
    return [r["seat_id"] for r in rows]


async def promote(conn, seat_ids):
    """Seat reallocation: hand each freed seat to the longest-waiting user, atomically."""
    for sid in seat_ids:
        w = await one(conn, "SELECT id, user_id FROM waitlist WHERE status='WAITING' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED")
        if not w:
            return
        try:
            async with conn.transaction():  # savepoint
                b = await one(conn, "INSERT INTO bookings(user_id,seat_id,status,expires_at) "
                                    "VALUES(%s,%s,'HELD',now()+make_interval(secs=>%s)) RETURNING id", w["user_id"], sid, HOLD)
                await conn.execute("UPDATE waitlist SET status='PROMOTED', booking_id=%s WHERE id=%s", (b["id"], w["id"]))
        except errors.UniqueViolation:
            pass  # someone else already holds it


async def booking_view(conn, bid):
    return await one(conn, f"""
        SELECT b.id, b.user_id, b.seat_id, s.label AS seat, {EFF} AS status, b.expires_at,
               GREATEST(0, EXTRACT(EPOCH FROM b.expires_at-now()))::int AS seconds_left, b.created_at,
               (SELECT status FROM payments p WHERE p.booking_id=b.id ORDER BY p.id DESC LIMIT 1) AS payment_status
        FROM bookings b JOIN seats s ON s.id=b.seat_id WHERE b.id=%s""", bid)


# ---------- background sweeper: settles delayed payments + expires holds ----------
async def sweep_once():
    async with pool.connection() as conn, conn.transaction():
        for p in await many(conn, "SELECT id, booking_id, final_outcome FROM payments WHERE status='PENDING' "
                                  "AND settle_at<=now() ORDER BY id LIMIT 200 FOR UPDATE SKIP LOCKED"):
            ok = p["final_outcome"] == "success"
            await conn.execute("UPDATE payments SET status=%s, updated_at=now() WHERE id=%s", ("SUCCESS" if ok else "FAILED", p["id"]))
            if ok:
                await conn.execute("UPDATE bookings SET status='CONFIRMED', updated_at=now() WHERE id=%s AND status='PENDING_PAYMENT'", (p["booking_id"],))
            else:  # failed: back to HELD with a fresh window so the user can retry
                await conn.execute("UPDATE bookings SET status='HELD', expires_at=now()+make_interval(secs=>%s), updated_at=now() "
                                   "WHERE id=%s AND status='PENDING_PAYMENT'", (HOLD, p["booking_id"]))
        await promote(conn, await expire_stale(conn))


async def sweeper():
    while True:
        try:
            await sweep_once()
        except Exception as e:  # keep the loop alive
            print("sweeper error:", e)
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_):
    await pool.open()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(42)")  # several workers may boot at once
        await conn.execute(open(os.path.join(os.path.dirname(__file__), "schema.sql")).read())
        await conn.execute("INSERT INTO seats(id,label) SELECT i, chr(65+(i-1)/20)||((i-1)%%20+1) "
                           "FROM generate_series(1,%s) i ON CONFLICT DO NOTHING", (SEATS,))
    task = asyncio.create_task(sweeper())
    yield
    task.cancel()
    await pool.close()


app = FastAPI(title="Concert Ticket Booking", lifespan=lifespan)


# ---------- models ----------
class UserIn(BaseModel):
    name: str = Field(min_length=1)
    email: EmailStr


class ReserveIn(BaseModel):
    user_id: int
    seat_id: int


class WaitIn(BaseModel):
    user_id: int


class PayIn(BaseModel):
    mode: Literal["success", "fail", "delayed"] = "success"
    delay_seconds: int = Field(5, ge=1, le=60)                 # only for mode=delayed
    delayed_outcome: Literal["success", "fail"] = "success"    # only for mode=delayed


# ---------- endpoints ----------
@app.get("/health")
async def health():
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
    return {"ok": True}


@app.post("/users", status_code=201)
async def create_user(u: UserIn):
    async with pool.connection() as conn:
        return await one(conn, "INSERT INTO users(name,email) VALUES(%s,%s) ON CONFLICT(email) DO UPDATE SET name=EXCLUDED.name "
                               "RETURNING id,name,email", u.name, u.email)


@app.get("/seats")
async def list_seats(status: Literal["AVAILABLE", "HELD", "BOOKED"] | None = None):
    async with pool.connection() as conn:
        rows = await many(conn, f"""
            SELECT s.id, s.label, s.price,
              CASE WHEN b.status='CONFIRMED' THEN 'BOOKED'
                   WHEN b.id IS NOT NULL AND (b.status='PENDING_PAYMENT' OR b.expires_at>now()) THEN 'HELD'
                   ELSE 'AVAILABLE' END AS status
            FROM seats s LEFT JOIN bookings b ON b.seat_id=s.id AND b.status IN {LIVE} ORDER BY s.id""")
    return [r for r in rows if not status or r["status"] == status]


@app.post("/reservations", status_code=201)
async def reserve(body: ReserveIn, idempotency_key: str | None = Header(None)):
    async with pool.connection() as conn, conn.transaction():
        if (replay := await idem_begin(conn, "reserve", idempotency_key)):
            return replay
        # free this seat if its previous hold timed out; waitlisted users get first claim
        await promote(conn, await expire_stale(conn, body.seat_id))
        try:
            async with conn.transaction():
                b = await one(conn, "INSERT INTO bookings(user_id,seat_id,status,expires_at) "
                                    "VALUES(%s,%s,'HELD',now()+make_interval(secs=>%s)) RETURNING id", body.user_id, body.seat_id, HOLD)
        except errors.UniqueViolation:   # the partial unique index said no: seat is taken
            return await idem_end(conn, "reserve", idempotency_key, 409, {"detail": "Seat is not available"})
        except errors.ForeignKeyViolation:
            return await idem_end(conn, "reserve", idempotency_key, 404, {"detail": "Unknown user or seat"})
        return await idem_end(conn, "reserve", idempotency_key, 201, await booking_view(conn, b["id"]))


@app.get("/bookings/{bid}")
async def get_booking(bid: int):
    async with pool.connection() as conn:
        b = await booking_view(conn, bid)
    if not b:
        raise HTTPException(404, "Booking not found")
    return b


@app.post("/bookings/{bid}/pay")
async def pay(bid: int, body: PayIn, idempotency_key: str | None = Header(None)):
    scope = f"pay:{bid}"
    async with pool.connection() as conn, conn.transaction():
        if (replay := await idem_begin(conn, scope, idempotency_key)):
            return replay
        b = await one(conn, "SELECT b.*, s.price, b.expires_at<=now() AS stale FROM bookings b JOIN seats s ON s.id=b.seat_id "
                            "WHERE b.id=%s FOR UPDATE OF b", bid)  # row lock serialises pay/cancel/expire per booking
        if not b:
            return await idem_end(conn, scope, idempotency_key, 404, {"detail": "Booking not found"})
        if b["status"] == "CONFIRMED":
            return await idem_end(conn, scope, idempotency_key, 200, await booking_view(conn, bid))
        if b["status"] == "PENDING_PAYMENT":
            return await idem_end(conn, scope, idempotency_key, 409, {"detail": "A payment is already in progress"})
        if b["status"] in ("EXPIRED", "CANCELLED") or b["stale"]:
            if b["status"] == "HELD":
                await conn.execute("UPDATE bookings SET status='EXPIRED', updated_at=now() WHERE id=%s", (bid,))
                await promote(conn, [b["seat_id"]])
            return await idem_end(conn, scope, idempotency_key, 410, {"detail": "Reservation expired or cancelled"})

        mode = body.mode
        pid = (await one(conn, "INSERT INTO payments(booking_id,amount,status,mode,settle_at,final_outcome) VALUES(%s,%s,'PENDING',%s,"
                               "CASE WHEN %s='delayed' THEN now()+make_interval(secs=>%s) END, %s) RETURNING id",
                         bid, b["price"], mode, mode, body.delay_seconds, body.delayed_outcome))["id"]
        if mode == "success":
            await conn.execute("UPDATE payments SET status='SUCCESS', updated_at=now() WHERE id=%s", (pid,))
            await conn.execute("UPDATE bookings SET status='CONFIRMED', updated_at=now() WHERE id=%s", (bid,))
            code = 200
        elif mode == "fail":
            await conn.execute("UPDATE payments SET status='FAILED', updated_at=now() WHERE id=%s", (pid,))
            code = 402   # booking stays HELD: user may retry until it expires
        else:            # delayed: seat is protected (PENDING_PAYMENT never expires) until the sweeper settles it
            await conn.execute("UPDATE bookings SET status='PENDING_PAYMENT', updated_at=now() WHERE id=%s", (bid,))
            code = 202
        return await idem_end(conn, scope, idempotency_key, code, await booking_view(conn, bid))


@app.post("/bookings/{bid}/cancel")
async def cancel(bid: int):
    async with pool.connection() as conn, conn.transaction():
        b = await one(conn, "SELECT * FROM bookings WHERE id=%s FOR UPDATE", bid)
        if not b:
            return JSONResponse({"detail": "Booking not found"}, 404)
        if b["status"] == "PENDING_PAYMENT":
            return JSONResponse({"detail": "Payment in progress; try again once it settles"}, 409)
        if b["status"] == "EXPIRED":
            return JSONResponse({"detail": "Reservation already expired"}, 409)
        if b["status"] != "CANCELLED":
            await conn.execute("UPDATE bookings SET status='CANCELLED', updated_at=now() WHERE id=%s", (bid,))
            await conn.execute("UPDATE payments SET status='REFUNDED', updated_at=now() WHERE booking_id=%s AND status='SUCCESS'", (bid,))
            await promote(conn, [b["seat_id"]])
        return await booking_view(conn, bid)


@app.get("/users/{uid}/bookings")
async def history(uid: int):
    async with pool.connection() as conn:
        return await many(conn, f"""
            SELECT b.id, s.label AS seat, {EFF} AS status, b.expires_at, b.created_at,
                   (SELECT status FROM payments p WHERE p.booking_id=b.id ORDER BY p.id DESC LIMIT 1) AS payment_status
            FROM bookings b JOIN seats s ON s.id=b.seat_id WHERE b.user_id=%s ORDER BY b.id DESC""", uid)


@app.post("/waitlist", status_code=201)
async def join_waitlist(body: WaitIn):
    async with pool.connection() as conn, conn.transaction():
        free = await one(conn, "SELECT count(*) AS n FROM seats s WHERE NOT EXISTS (SELECT 1 FROM bookings b WHERE b.seat_id=s.id "
                               "AND (b.status IN ('CONFIRMED','PENDING_PAYMENT') OR (b.status='HELD' AND b.expires_at>now())))")
        if free["n"] > 0:
            return JSONResponse({"detail": "Seats are available; reserve directly"}, 409)
        try:
            async with conn.transaction():
                w = await one(conn, "INSERT INTO waitlist(user_id) VALUES(%s) RETURNING id", body.user_id)
        except errors.UniqueViolation:
            return JSONResponse({"detail": "Already on the waitlist"}, 409)
        except errors.ForeignKeyViolation:
            return JSONResponse({"detail": "Unknown user"}, 404)
        return {"waitlist_id": w["id"]}


@app.get("/waitlist/{wid}")
async def waitlist_status(wid: int):
    async with pool.connection() as conn:
        w = await one(conn, "SELECT id,user_id,status,booking_id,"
                            "(SELECT count(*) FROM waitlist x WHERE x.status='WAITING' AND x.id<=w.id) AS position "
                            "FROM waitlist w WHERE id=%s", wid)
    if not w:
        raise HTTPException(404, "Not found")
    if w["status"] != "WAITING":
        w["position"] = None
    return w


@app.get("/stats")
async def stats():
    async with pool.connection() as conn:
        return await one(conn, """
            SELECT (SELECT count(*) FROM seats) AS total_seats,
              (SELECT count(*) FROM bookings WHERE status='CONFIRMED') AS booked,
              (SELECT count(*) FROM bookings WHERE status='PENDING_PAYMENT' OR (status='HELD' AND expires_at>now())) AS held,
              (SELECT count(*) FROM waitlist WHERE status='WAITING') AS waiting,
              (SELECT count(*) FROM (SELECT seat_id FROM bookings WHERE status IN ('HELD','PENDING_PAYMENT','CONFIRMED')
                                     GROUP BY seat_id HAVING count(*)>1) d) AS double_booked_seats""")
