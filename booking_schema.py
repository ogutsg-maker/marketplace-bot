"""Additional booking/payment/check-in schema for Armenia AI Guide.

This file intentionally extends the clean platform schema without reintroducing
the legacy orders/bids architecture.
"""
from __future__ import annotations
import os
import psycopg


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    return url


def ensure_booking_schema() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS bookings (
        id BIGSERIAL PRIMARY KEY,
        request_id BIGINT REFERENCES service_requests(id) ON DELETE SET NULL,
        negotiation_id BIGINT REFERENCES negotiations(id) ON DELETE SET NULL,
        client_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        service_id BIGINT REFERENCES services(id) ON DELETE SET NULL,
        package_id BIGINT REFERENCES service_packages(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN (
                'pending','awaiting_payment','paid','confirmed',
                'checked_in','completed','cancelled','disputed','expired'
            )),
        service_name TEXT NOT NULL DEFAULT '',
        agreed_price NUMERIC NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'AMD',
        commission_amount NUMERIC NOT NULL DEFAULT 0,
        partner_amount NUMERIC NOT NULL DEFAULT 0,
        scheduled_at TIMESTAMPTZ,
        client_note TEXT NOT NULL DEFAULT '',
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_bookings_partner_status
        ON bookings(partner_id, status, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_bookings_client_status
        ON bookings(client_id, status, updated_at DESC);

    CREATE TABLE IF NOT EXISTS payments (
        id BIGSERIAL PRIMARY KEY,
        booking_id BIGINT REFERENCES bookings(id) ON DELETE CASCADE,
        client_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
        partner_id BIGINT REFERENCES partners(id) ON DELETE SET NULL,
        payment_type TEXT NOT NULL DEFAULT 'commission'
            CHECK (payment_type IN ('commission','premium_contact','refund','other')),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','paid','failed','refunded','expired')),
        amount NUMERIC NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'AMD',
        provider TEXT,
        provider_payment_id TEXT,
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_payments_booking ON payments(booking_id);
    CREATE INDEX IF NOT EXISTS idx_payments_partner ON payments(partner_id, status);

    CREATE TABLE IF NOT EXISTS booking_checkins (
        id BIGSERIAL PRIMARY KEY,
        booking_id BIGINT NOT NULL UNIQUE REFERENCES bookings(id) ON DELETE CASCADE,
        token TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active','used','cancelled','expired')),
        issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        checked_in_at TIMESTAMPTZ,
        checked_in_by BIGINT,
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb
    );

    CREATE TABLE IF NOT EXISTS booking_cancellations (
        id BIGSERIAL PRIMARY KEY,
        booking_id BIGINT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
        cancelled_by TEXT NOT NULL DEFAULT 'system',
        reason TEXT NOT NULL DEFAULT '',
        refund_amount NUMERIC NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS partner_financial_ledger (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        booking_id BIGINT REFERENCES bookings(id) ON DELETE SET NULL,
        entry_type TEXT NOT NULL
            CHECK (entry_type IN ('sale','commission','refund','adjustment')),
        amount NUMERIC NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'AMD',
        description TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_partner_ledger
        ON partner_financial_ledger(partner_id, created_at DESC);

    CREATE TABLE IF NOT EXISTS contact_disclosures (
        id BIGSERIAL PRIMARY KEY,
        client_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        payment_id BIGINT REFERENCES payments(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','paid','approved','rejected','expired','refunded')),
        fee_amount NUMERIC NOT NULL DEFAULT 0,
        disclosure_scope TEXT NOT NULL DEFAULT 'limited',
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_contact_disclosures_client
        ON contact_disclosures(client_id, created_at DESC);
    """
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
