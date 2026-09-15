"""Small runtime compatibility migrations loaded automatically by Python."""
import os


def _run_compat_migrations():
    """Repair legacy PostgreSQL schemas before application routes are used.

    This is intentionally additive/non-destructive. Older deployments created
    admin_audit_log with a different column name; Stage 3 now writes the
    canonical admin_telegram_id column. Keep both schemas compatible.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return
    try:
        import psycopg
        with psycopg.connect(url, prepare_threshold=None) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS admin_audit_log (
                        id BIGSERIAL PRIMARY KEY,
                        admin_telegram_id BIGINT,
                        action TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_id BIGINT,
                        details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    ALTER TABLE admin_audit_log
                    ADD COLUMN IF NOT EXISTS admin_telegram_id BIGINT
                """)
                cur.execute("""
                    ALTER TABLE admin_audit_log
                    ADD COLUMN IF NOT EXISTS action TEXT
                """)
                cur.execute("""
                    ALTER TABLE admin_audit_log
                    ADD COLUMN IF NOT EXISTS entity_type TEXT
                """)
                cur.execute("""
                    ALTER TABLE admin_audit_log
                    ADD COLUMN IF NOT EXISTS entity_id BIGINT
                """)
                cur.execute("""
                    ALTER TABLE admin_audit_log
                    ADD COLUMN IF NOT EXISTS details_json JSONB DEFAULT '{}'::jsonb
                """)
                cur.execute("""
                    ALTER TABLE admin_audit_log
                    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()
                """)
            conn.commit()
    except Exception as exc:
        # Never prevent the HTTP server/bot from starting because of this
        # compatibility migration. The normal application migration can retry.
        print(f"[sitecustomize] compatibility migration skipped: {exc!r}", flush=True)


_run_compat_migrations()
