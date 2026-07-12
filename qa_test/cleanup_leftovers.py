"""Clean up any leftover transactions on the test user_id."""
import io, os, sys, hmac, hashlib, time, json, requests
from urllib.parse import urlencode

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE_URL = "https://worker-production-68b3.up.railway.app"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required for production QA cleanup")
TEST_USER_ID = 999000777


def make_init_data(user_id, token):
    user_json = json.dumps({"id": user_id, "first_name": "QAv2", "username": "qa2"}, separators=(",", ":"))
    params = {"auth_date": str(int(time.time())), "user": user_json, "query_id": "QA"}
    pairs = sorted(f"{k}={v}" for k, v in params.items())
    data_check = "\n".join(pairs)
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    params["hash"] = h
    return urlencode(params)


HEADERS = {"X-Telegram-Init-Data": make_init_data(TEST_USER_ID, BOT_TOKEN), "Content-Type": "application/json"}

r = requests.get(f"{BASE_URL}/api/transactions?limit=all", headers=HEADERS, timeout=30)
rows = r.json() if r.headers.get("content-type", "").startswith("application/json") else []
print(f"Found {len(rows)} existing transactions on user {TEST_USER_ID}")
for row in rows:
    tid = row.get("id")
    time.sleep(1.05)
    d = requests.delete(f"{BASE_URL}/api/transactions/{tid}", headers=HEADERS, timeout=30)
    print(f"  DELETE {tid} type={row.get('type')} amt={row.get('amount')} cat={row.get('category')} -> {d.status_code}")

time.sleep(1.05)
r2 = requests.get(f"{BASE_URL}/api/transactions?limit=all", headers=HEADERS, timeout=30)
left = r2.json() if r2.headers.get("content-type", "").startswith("application/json") else []
print(f"After cleanup: {len(left)} transactions remain")
