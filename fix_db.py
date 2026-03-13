"""
fix_db.py — One-time migration to add all missing columns to an existing jobs table.

Run this ONCE if you already had a 'jobs' table from a previous project:
  py fix_db.py

It is fully idempotent — safe to run multiple times.
"""
import logging
import sys

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fix_db")


ALL_JOB_COLUMNS = [
    # (column_name,  sql_definition)
    ("location",         "VARCHAR(300)"),
    ("salary",           "VARCHAR(200)"),
    ("date_posted",      "VARCHAR(100)"),
    ("description",      "TEXT"),
    ("source",           "VARCHAR(50)"),
    ("is_active",        "BOOLEAN DEFAULT TRUE"),
    ("first_seen",       "TIMESTAMP DEFAULT now()"),
    ("last_seen",        "TIMESTAMP DEFAULT now()"),
    ("legitimacy_score", "INTEGER"),
    ("score_breakdown",  "JSONB"),
    ("suspected_ghost",  "BOOLEAN DEFAULT FALSE"),
    ("tg_alerted",       "BOOLEAN DEFAULT FALSE"),
    ("ai_reasoning",     "TEXT"),
]


def fix_jobs_table(engine):
    from sqlalchemy import inspect, text

    inspector = inspect(engine)

    if "jobs" not in inspector.get_table_names():
        logger.info("No existing 'jobs' table — nothing to fix (it will be created fresh).")
        return

    existing_cols = {col["name"] for col in inspector.get_columns("jobs")}
    logger.info("Existing columns: %s", sorted(existing_cols))

    added = []
    with engine.begin() as conn:
        for col_name, col_def in ALL_JOB_COLUMNS:
            if col_name not in existing_cols:
                sql = f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {col_name} {col_def}"
                conn.execute(text(sql))
                added.append(col_name)
                logger.info("  ✓ Added column: jobs.%s", col_name)
            else:
                logger.info("  - Already exists: jobs.%s", col_name)

    if added:
        logger.info("Migration complete. Added %d column(s): %s", len(added), added)
    else:
        logger.info("All columns already present — no changes needed.")


def fix_tracker_table(engine):
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS application_tracker (
                id         SERIAL PRIMARY KEY,
                job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                status     VARCHAR(50) DEFAULT 'saved',
                notes      TEXT,
                applied_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT now()
            )
        """))
    logger.info("application_tracker table: OK")


def main():
    from core.database import check_connection, get_engine

    if not check_connection():
        logger.error("Cannot connect to database. Is PostgreSQL running?")
        logger.error("Check: DATABASE_URL in .env = postgresql://jobsuser:jobspass@localhost:5432/jobsdb")
        sys.exit(1)

    engine = get_engine()
    fix_jobs_table(engine)
    fix_tracker_table(engine)

    logger.info("All done! Now re-run: py run_all.py --score")


if __name__ == "__main__":
    main()
