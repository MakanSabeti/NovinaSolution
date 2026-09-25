import os
import socket
import sys
import time

from dotenv import load_dotenv

load_dotenv()

url = os.environ.get("DATABASE_URL")
if not url:
    sys.exit("DATABASE_URL is not set. Is there a backend/.env file (not just .env.example)?")

if "REPLACE_WITH_YOUR_DB_PASSWORD" in url or "[YOUR-PASSWORD]" in url:
    sys.exit("DATABASE_URL still has the placeholder password in it -- edit backend/.env and put your real Supabase database password in place of REPLACE_WITH_YOUR_DB_PASSWORD.")

# Pull host/port out of the URL without importing psycopg yet.
import urllib.parse as up
parsed = up.urlparse(url)
host, port = parsed.hostname, parsed.port or 6543
print(f"Target: {host}:{port}  (user={parsed.username})")

print("\n[1/2] Raw TCP connect (proves the network path is open) ...")
try:
    t0 = time.time()
    s = socket.create_connection((host, port), timeout=10)
    s.close()
    print(f"    OK ({time.time()-t0:.1f}s) -- TCP reaches Supabase on this network.")
except Exception as e:
    print(f"    FAILED: {type(e).__name__}: {e}")
    print("    -> This is a network problem (firewall, VPN, or captive network), not a code")
    print("       or credentials problem. Try a different network, or check with IT about")
    print("       outbound access on port 6543.")
    sys.exit(1)

print("\n[2/2] Real Postgres connection (proves credentials + database) ...")
try:
    import psycopg
except ImportError:
    sys.exit("psycopg isn't installed -- run: pip install -r requirements.txt")

try:
    t0 = time.time()
    with psycopg.connect(url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            print(f"    OK ({time.time()-t0:.1f}s): {cur.fetchone()[0]}")
    print("\nConnection is fully working. The pool timeout was likely transient --")
    print("try running the smoke test again.")
except Exception as e:
    print(f"    FAILED: {type(e).__name__}: {e}")
    print("\n    Common causes:")
    print("    - Wrong password (reset it in Supabase: Project Settings -> Database)")
    print("    - Password has special characters (@ : / # ?) that need URL-encoding")
    print("    - Project is paused (free-tier projects pause after inactivity -- open")
    print("      the Supabase dashboard once to wake it up)")
    sys.exit(1)
