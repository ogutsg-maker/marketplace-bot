import os
import psycopg
from dotenv import load_dotenv

load_dotenv()

conn = psycopg.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

cur.execute("""
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public'
    ORDER BY table_name
""")

for row in cur.fetchall():
    print(row[0])

cur.close()
conn.close()