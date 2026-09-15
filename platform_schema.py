"""Non-destructive PostgreSQL schema for the new Armenia AI Guide architecture."""
from __future__ import annotations

import os
import psycopg


def db_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")
    return value


def _connect():
    # Render/Supabase may use PgBouncer transaction pooling. Disable Psycopg
    # automatic prepared statements for schema/bootstrap work.
    return psycopg.connect(db_url(), prepare_threshold=None)


def ensure_platform_schema() -> None:
    """Create only new architecture tables; never delete legacy marketplace data."""
    sql = r'''
    CREATE TABLE IF NOT EXISTS partners (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL UNIQUE REFERENCES users(telegram_id) ON DELETE CASCADE,
        business_name TEXT NOT NULL DEFAULT '',
        business_description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','pending','under_review','approved','rejected','suspended','blocked')),
        verification_status TEXT NOT NULL DEFAULT 'not_submitted'
            CHECK (verification_status IN ('not_submitted','pending','approved','rejected')),
        rejection_reason TEXT,
        contact_share_policy TEXT NOT NULL DEFAULT 'after_booking'
            CHECK (contact_share_policy IN ('after_booking','premium','never')),
        contact_sharing_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        premium_contact_sharing_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        profile_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS partner_locations (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        country TEXT NOT NULL DEFAULT 'Armenia',
        marz TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '',
        village TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
        location_type TEXT NOT NULL DEFAULT 'city', data_json JSONB NOT NULL DEFAULT '{}'::jsonb
    );

    CREATE TABLE IF NOT EXISTS catalog_subcategories (
        id BIGSERIAL PRIMARY KEY,
        category_id BIGINT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
        name_am TEXT NOT NULL DEFAULT '', name_ru TEXT NOT NULL DEFAULT '',
        name_en TEXT NOT NULL DEFAULT '', slug TEXT NOT NULL DEFAULT '',
        active BOOLEAN NOT NULL DEFAULT TRUE,
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb
    );

    CREATE TABLE IF NOT EXISTS partner_objects (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        name TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
        location_id BIGINT REFERENCES partner_locations(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'draft', data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS partner_services (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        service_id BIGINT, object_id BIGINT REFERENCES partner_objects(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'draft', data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS services (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        category_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        subcategory_id BIGINT REFERENCES catalog_subcategories(id) ON DELETE SET NULL,
        name TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
        price NUMERIC, currency TEXT NOT NULL DEFAULT 'AMD', duration_minutes INTEGER,
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','pending','approved','rejected','frozen','deleted')),
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS service_packages (
        id BIGSERIAL PRIMARY KEY,
        service_id BIGINT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
        name TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
        price NUMERIC DEFAULT 0, currency TEXT NOT NULL DEFAULT 'AMD',
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb
    );

    CREATE TABLE IF NOT EXISTS service_options (
        id BIGSERIAL PRIMARY KEY,
        service_id BIGINT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
        name TEXT NOT NULL DEFAULT '', price_delta NUMERIC DEFAULT 0,
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb
    );

    CREATE TABLE IF NOT EXISTS service_schedule (
        id BIGSERIAL PRIMARY KEY,
        service_id BIGINT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
        weekday INTEGER, start_time TIME, end_time TIME, data_json JSONB NOT NULL DEFAULT '{}'::jsonb
    );

    CREATE TABLE IF NOT EXISTS potential_partners (
        id BIGSERIAL PRIMARY KEY,
        category_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        subcategory_id BIGINT REFERENCES catalog_subcategories(id) ON DELETE SET NULL,
        business_name TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '', source_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'new', data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS service_requests (
        id BIGSERIAL PRIMARY KEY,
        client_id BIGINT NOT NULL,
        category_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        subcategory_id BIGINT REFERENCES catalog_subcategories(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'discovery', language TEXT NOT NULL DEFAULT 'hy',
        city TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
        preferences_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS request_candidates (
        id BIGSERIAL PRIMARY KEY,
        request_id BIGINT NOT NULL REFERENCES service_requests(id) ON DELETE CASCADE,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        service_id BIGINT REFERENCES services(id) ON DELETE SET NULL,
        rank_score NUMERIC DEFAULT 0, match_reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'suggested',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS negotiations (
        id BIGSERIAL PRIMARY KEY,
        request_id BIGINT NOT NULL REFERENCES service_requests(id) ON DELETE CASCADE,
        client_id BIGINT NOT NULL, partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'active', state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS negotiation_messages (
        id BIGSERIAL PRIMARY KEY,
        negotiation_id BIGINT NOT NULL REFERENCES negotiations(id) ON DELETE CASCADE,
        sender_role TEXT NOT NULL, sender_id BIGINT, message TEXT NOT NULL DEFAULT '',
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_services_partner_status ON services(partner_id, status);
    CREATE INDEX IF NOT EXISTS idx_services_catalog ON services(category_id, subcategory_id, status);
    CREATE INDEX IF NOT EXISTS idx_partner_directions_lookup ON partner_directions(partner_id, status);
    CREATE INDEX IF NOT EXISTS idx_requests_client_status ON service_requests(client_id, status, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_negotiations_client ON negotiations(client_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_negotiations_partner ON negotiations(partner_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_negotiation_messages ON negotiation_messages(negotiation_id, created_at);
    '''

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
