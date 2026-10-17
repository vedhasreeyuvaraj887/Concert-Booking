CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS seats (
  id INT PRIMARY KEY, label TEXT UNIQUE NOT NULL, price NUMERIC(8,2) NOT NULL DEFAULT 500);

CREATE TABLE IF NOT EXISTS bookings (
  id BIGSERIAL PRIMARY KEY,
  user_id INT NOT NULL REFERENCES users(id),
  seat_id INT NOT NULL REFERENCES seats(id),
  status TEXT NOT NULL CHECK (status IN ('HELD','PENDING_PAYMENT','CONFIRMED','EXPIRED','CANCELLED')),
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now());

-- THE core guarantee: at most one live booking per seat, enforced by the database itself.
CREATE UNIQUE INDEX IF NOT EXISTS one_live_booking_per_seat
  ON bookings(seat_id) WHERE status IN ('HELD','PENDING_PAYMENT','CONFIRMED');
CREATE INDEX IF NOT EXISTS bookings_user_idx ON bookings(user_id);
CREATE INDEX IF NOT EXISTS bookings_expiry_idx ON bookings(expires_at) WHERE status = 'HELD';

CREATE TABLE IF NOT EXISTS payments (
  id BIGSERIAL PRIMARY KEY,
  booking_id BIGINT NOT NULL REFERENCES bookings(id),
  amount NUMERIC(8,2) NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING','SUCCESS','FAILED','REFUNDED')),
  mode TEXT NOT NULL,
  settle_at TIMESTAMPTZ,            -- delayed payments: when the "gateway" answers
  final_outcome TEXT,               -- 'success' | 'fail' for delayed payments
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS payments_booking_idx ON payments(booking_id);
CREATE INDEX IF NOT EXISTS payments_pending_idx ON payments(settle_at) WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS idempotency_keys (
  scope TEXT NOT NULL, key TEXT NOT NULL, status_code INT, response JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (scope, key));

CREATE TABLE IF NOT EXISTS waitlist (
  id BIGSERIAL PRIMARY KEY,
  user_id INT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'WAITING' CHECK (status IN ('WAITING','PROMOTED')),
  booking_id BIGINT REFERENCES bookings(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS one_waiting_entry_per_user ON waitlist(user_id) WHERE status = 'WAITING';
