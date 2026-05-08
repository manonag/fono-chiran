"""
Apply a CHIRAN migration file inside a single transaction.
Usage: python migrations/run_migration.py <migration_file.sql>
"""
import os
import sys
import psycopg2


def main():
    if len(sys.argv) != 2:
        print("Usage: python migrations/run_migration.py <migration_file.sql>")
        sys.exit(1)

    migration_path = sys.argv[1]
    if not os.path.exists(migration_path):
        print(f"Migration file not found: {migration_path}")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set in environment")
        sys.exit(1)

    with open(migration_path, "r") as f:
        sql = f.read()

    print(f"Applying migration: {migration_path}")
    conn = psycopg2.connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        print("Migration applied successfully.")
    except Exception as e:
        print(f"Migration failed: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
