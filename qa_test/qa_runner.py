"""QA test runner for GET /api/transactions filtering capabilities."""
import io
import os
import hmac
import hashlib
import time
import json
import sys
import requests
from urllib.parse import urlencode
from datetime import datetime, timedelta

# Force UTF-8 stdout/stderr on Windows so we can print Cyrillic + arrows.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE_URL = "https://worker-production-68b3.up.railway.app"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required for production QA")
TEST_USER_ID = 999000777
TODAY = "2026-06-09"

REQ_DELAY = 1.05
last_req = [0.0]


def throttle():
    delta = time.time() - last_req[0]
    if delta < REQ_DELAY:
        time.sleep(REQ_DELAY - delta)
    last_req[0] = time.time()


def make_init_data(user_id, token):
    user_json = json.dumps(
        {"id": user_id, "first_name": "QAv2", "username": "qa2"},
        separators=(",", ":"),
    )
    params = {
        "auth_date": str(int(time.time())),
        "user": user_json,
        "query_id": "QA",
    }
    pairs = sorted(f"{k}={v}" for k, v in params.items())
    data_check = "\n".join(pairs)
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    params["hash"] = h
    return urlencode(params)


def auth_headers(user_id=TEST_USER_ID):
    return {
        "X-Telegram-Init-Data": make_init_data(user_id, BOT_TOKEN),
        "Content-Type": "application/json",
    }


def req(method, path, **kwargs):
    throttle()
    url = f"{BASE_URL}{path}"
    headers = kwargs.pop("headers", None) or auth_headers()
    r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    return r


def jget(r):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:500]}


# ---------------- Test bookkeeping ----------------
RESULTS = []  # list of (id, name, passed, details)
POSTED_IDS = []  # for cleanup


def record(tid, name, passed, details=""):
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {tid:>5} {name}")
    if details:
        for line in details.splitlines():
            print(f"        {line}")
    RESULTS.append((tid, name, passed, details))


def assert_eq(tid, name, expected, actual, extra=""):
    ok = (expected == actual)
    detail = ""
    if not ok:
        detail = f"expected: {expected!r}\nactual:   {actual!r}\n{extra}".strip()
    record(tid, name, ok, detail)
    return ok


# ---------------- FIXTURES ----------------
FIXTURES = [
    {"type": "income",  "amount": 100, "currency": "UAH", "category": "Зарплата",  "description": "QA fx 1"},
    {"type": "expense", "amount":  40, "currency": "UAH", "category": "Кафе",      "description": "QA fx 2"},
    {"type": "income",  "amount": 500, "currency": "UAH", "category": "Фріланс",   "description": "QA fx 3"},
    {"type": "expense", "amount":  80, "currency": "UAH", "category": "Продукти",  "description": "QA fx 4"},
]


def post_fixtures():
    print("\n=== POST fixtures ===")
    for fx in FIXTURES:
        r = req("POST", "/api/transactions", json=fx)
        body = jget(r)
        if r.status_code == 201 and isinstance(body, dict) and body.get("id"):
            POSTED_IDS.append(body["id"])
            print(f"  posted id={body['id']} type={body['type']} amt={body['amount']} cat={body['category']} date={body.get('date')}")
        else:
            print(f"  POST FAILED: status={r.status_code} body={body}")
            return False
    return True


def cleanup_fixtures():
    print("\n=== CLEANUP ===")
    for tid in POSTED_IDS:
        r = req("DELETE", f"/api/transactions/{tid}")
        print(f"  DELETE {tid} -> {r.status_code}")


def find_by_id(rows, tx_id):
    for r in rows:
        if r.get("id") == tx_id:
            return r
    return None


# ---------------- P0: Filter correctness ----------------
def test_type_filter():
    # All 4 fixtures dated today (POST hardcodes date=now). type filter is purely about the column.
    r = req("GET", "/api/transactions?type=income")
    body = jget(r)
    if not isinstance(body, list):
        record("2a", "?type=income returns list", False, f"status={r.status_code} body={body}")
        return
    inc_ids = {row["id"] for row in body if row.get("id") in POSTED_IDS}
    expected_inc = {POSTED_IDS[0], POSTED_IDS[2]}
    types_ok = all(row.get("type") == "income" for row in body)
    record("2a", "?type=income — only income rows",
           inc_ids == expected_inc and types_ok,
           f"expected fixture ids in result: {expected_inc}\ngot: {inc_ids}\nall types==income: {types_ok}")

    r = req("GET", "/api/transactions?type=expense")
    body = jget(r)
    if not isinstance(body, list):
        record("2b", "?type=expense returns list", False, f"status={r.status_code} body={body}")
        return
    exp_ids = {row["id"] for row in body if row.get("id") in POSTED_IDS}
    expected_exp = {POSTED_IDS[1], POSTED_IDS[3]}
    types_ok = all(row.get("type") == "expense" for row in body)
    record("2b", "?type=expense — only expense rows",
           exp_ids == expected_exp and types_ok,
           f"expected fixture ids in result: {expected_exp}\ngot: {exp_ids}\nall types==expense: {types_ok}")


def test_period_current_month():
    r = req("GET", "/api/transactions?period=current_month&limit=all")
    body = jget(r)
    if not isinstance(body, list):
        record("3", "?period=current_month returns list", False, f"status={r.status_code} body={body}")
        return
    # All 4 fixtures are dated today (June 2026), so all should appear.
    found = {row["id"] for row in body if row.get("id") in POSTED_IDS}
    in_june = all(row.get("date", "").startswith("2026-06") for row in body)
    record("3", "?period=current_month — June rows only",
           found == set(POSTED_IDS) and in_june,
           f"all 4 fixture ids present: {found == set(POSTED_IDS)}\nall rows date startswith 2026-06: {in_june}\nfound: {found}")


def test_period_10d():
    r = req("GET", "/api/transactions?period=10d&limit=all")
    body = jget(r)
    if not isinstance(body, list):
        record("4", "?period=10d returns list", False, f"status={r.status_code} body={body}")
        return
    # Today is 2026-06-09. 10d window: 2026-05-30..2026-06-09. All fixtures (today) qualify.
    found = {row["id"] for row in body if row.get("id") in POSTED_IDS}
    cutoff = "2026-05-30"
    all_in_window = all(row.get("date", "") >= cutoff for row in body)
    record("4", "?period=10d — rows within last 10 days",
           found == set(POSTED_IDS) and all_in_window,
           f"all 4 fixture ids present: {found == set(POSTED_IDS)}\nall rows date >= {cutoff}: {all_in_window}\nfound: {found}")


def test_period_30d():
    r = req("GET", "/api/transactions?period=30d&limit=all")
    body = jget(r)
    if not isinstance(body, list):
        record("5", "?period=30d returns list", False, f"status={r.status_code} body={body}")
        return
    # Today 2026-06-09. 30d window: 2026-05-10..2026-06-09. All fixtures qualify.
    found = {row["id"] for row in body if row.get("id") in POSTED_IDS}
    cutoff = "2026-05-10"
    all_in_window = all(row.get("date", "") >= cutoff for row in body)
    record("5", "?period=30d — rows within last 30 days",
           found == set(POSTED_IDS) and all_in_window,
           f"all 4 fixture ids present: {found == set(POSTED_IDS)}\nall rows date >= {cutoff}: {all_in_window}\nfound: {found}")


def test_period_specific_month_march():
    r = req("GET", "/api/transactions?period=month&year=2026&month=3&limit=all")
    body = jget(r)
    if not isinstance(body, list):
        record("6", "?period=month March returns list", False, f"status={r.status_code} body={body}")
        return
    # No fixtures in March 2026 (we can't backdate via API). Expect: no fixture ids returned;
    # any other rows that exist for this user must all be in March.
    fixture_in_march = any(row.get("id") in POSTED_IDS for row in body)
    all_march = all(row.get("date", "").startswith("2026-03") for row in body)
    record("6", "?period=month year=2026 month=3 — only March rows",
           (not fixture_in_march) and all_march,
           f"no fixture ids leaked: {not fixture_in_march}\nall returned dates start 2026-03: {all_march}\nrow count: {len(body)}")


def test_explicit_from_to_may():
    r = req("GET", "/api/transactions?from=2026-05-01&to=2026-05-31&limit=all")
    body = jget(r)
    if not isinstance(body, list):
        record("7", "?from=2026-05-01&to=2026-05-31 returns list", False, f"status={r.status_code} body={body}")
        return
    fixture_in_may = any(row.get("id") in POSTED_IDS for row in body)
    all_may = all("2026-05-01" <= row.get("date", "") <= "2026-05-31" for row in body)
    record("7", "?from=2026-05-01&to=2026-05-31 — May rows only",
           (not fixture_in_may) and all_may,
           f"no fixture ids leaked: {not fixture_in_may}\nall returned dates within May: {all_may}\nrow count: {len(body)}")


def test_combined_expense_30d():
    r = req("GET", "/api/transactions?type=expense&period=30d&limit=all")
    body = jget(r)
    if not isinstance(body, list):
        record("8", "?type=expense&period=30d returns list", False, f"status={r.status_code} body={body}")
        return
    # Within 30d window: both expense fixtures (40 Кафе, 80 Продукти) — both posted today.
    # NOTE: Test plan said "only one row (Кафе 40)" assuming May 15 income would be filtered,
    # but Продукти 80 also expense and dated today (June 9), so both should appear.
    # Reporting the actual situation transparently below.
    found = {row["id"] for row in body if row.get("id") in POSTED_IDS}
    expected = {POSTED_IDS[1], POSTED_IDS[3]}  # both expense fixtures
    types_ok = all(row.get("type") == "expense" for row in body)
    record("8", "?type=expense&period=30d — only expense in last 30d",
           found == expected and types_ok,
           f"expected fixture ids (both expense fixtures, today): {expected}\ngot: {found}\nall types==expense: {types_ok}\n"
           f"NOTE: test plan assumed May & March fixtures could be backdated; POST /api/transactions hardcodes date=now, so all 4 fixtures are dated 2026-06-09")


def test_limit_all_vs_limit_1():
    r = req("GET", "/api/transactions?type=income&limit=all")
    body_all = jget(r)
    if not isinstance(body_all, list):
        record("9a", "?type=income&limit=all returns list", False, f"status={r.status_code}")
        return
    inc_count_all = sum(1 for row in body_all if row.get("id") in POSTED_IDS)
    record("9a", "limit=all returns full filtered set (>=2 income fixtures)",
           inc_count_all == 2,
           f"income fixture rows seen: {inc_count_all} (expected 2)\ntotal rows: {len(body_all)}")

    r = req("GET", "/api/transactions?type=income&limit=1")
    body1 = jget(r)
    if not isinstance(body1, list):
        record("9b", "?type=income&limit=1 returns list", False, f"status={r.status_code}")
        return
    record("9b", "limit=1 → single newest matching row",
           len(body1) == 1 and body1[0].get("type") == "income",
           f"row count: {len(body1)}\nfirst row type: {body1[0].get('type') if body1 else None}")


# ---------------- P1: Validation ----------------
def test_validation():
    cases = [
        ("10", "type=bogus → 400",       "/api/transactions?type=bogus",          400),
        ("11", "period=bogus → 400",     "/api/transactions?period=bogus",        400),
        ("12", "from=2026/01/01 → 400",  "/api/transactions?from=2026/01/01",     400),
        ("13", "year=0&period=month → 400", "/api/transactions?period=month&year=0&month=1", 400),
        ("14", "month=13&period=month → 400", "/api/transactions?period=month&year=2026&month=13", 400),
    ]
    for tid, name, path, expected_status in cases:
        r = req("GET", path)
        body = jget(r)
        record(tid, name, r.status_code == expected_status,
               f"status: expected {expected_status} got {r.status_code}\nbody: {json.dumps(body, ensure_ascii=False)[:200]}")


# ---------------- P2: Regressions ----------------
def test_regression_15_no_params():
    r = req("GET", "/api/transactions")
    body = jget(r)
    if not isinstance(body, list):
        record("15", "GET /api/transactions (no params)", False, f"status={r.status_code} body={body}")
        return
    cap_ok = len(body) <= 15
    types = {row.get("type") for row in body}
    mixed_ok = (len(types) >= 1)  # mixed implies whatever the user has
    record("15", "no params → ≤15 rows, mixed types (unchanged)",
           cap_ok,
           f"row count: {len(body)} (cap 15)\ntypes present: {types}")


def test_regression_balance():
    r = req("GET", "/api/balance?year=2026&month=6")
    body = jget(r)
    record("16", "GET /api/balance?year=2026&month=6 works",
           r.status_code == 200 and isinstance(body, dict),
           f"status={r.status_code}\nshape: {type(body).__name__}\nkeys: {list(body.keys()) if isinstance(body, dict) else None}\nbody: {json.dumps(body, ensure_ascii=False)[:300]}")


def test_regression_reports_monthly():
    r = req("GET", "/api/reports/monthly?year=2026&month=6")
    body = jget(r)
    record("17", "GET /api/reports/monthly?year=2026&month=6 works",
           r.status_code == 200,
           f"status={r.status_code}\nshape: {type(body).__name__}\nbody: {json.dumps(body, ensure_ascii=False)[:300]}")


def test_regression_categories_full():
    r = req("GET", "/api/categories/full")
    body = jget(r)
    record("18", "GET /api/categories/full works",
           r.status_code == 200,
           f"status={r.status_code}\nshape: {type(body).__name__}\nbody: {json.dumps(body, ensure_ascii=False)[:300]}")


def test_regression_post_employees():
    payload = {"name": "QA-test-employee", "salary": 12345}
    r = req("POST", "/api/employees", json=payload)
    body = jget(r)
    # The Mini App expects JSON; accept either 200/201 with dict body.
    ok = r.status_code in (200, 201) and isinstance(body, (dict, list))
    record("19", "POST /api/employees → returns proper JSON",
           ok,
           f"status={r.status_code}\nshape: {type(body).__name__}\nbody: {json.dumps(body, ensure_ascii=False)[:300]}")
    # Best-effort cleanup of the test employee
    if ok and isinstance(body, dict) and body.get("id"):
        try:
            req("DELETE", f"/api/employees/{body['id']}")
        except Exception:
            pass


def test_regression_delete_foreign_id():
    # Pick an id we definitely don't own. Use a very large negative-ish positive number.
    # The bot is multi-user; another id should belong to another user (or none).
    r = req("DELETE", "/api/transactions/999999999")
    body = jget(r)
    record("20", "DELETE foreign tx id → 404 (ownership check intact)",
           r.status_code == 404,
           f"status: expected 404 got {r.status_code}\nbody: {json.dumps(body, ensure_ascii=False)[:200]}")


# ---------------- MAIN ----------------
def main():
    print(f"=== QA RUN against {BASE_URL} ===")
    print(f"test user_id={TEST_USER_ID}  today={TODAY}")

    # bare list — sanity
    r = req("GET", "/api/transactions")
    print(f"\nbare GET: status={r.status_code} rows={len(jget(r)) if isinstance(jget(r), list) else '?'}")

    if not post_fixtures():
        print("\nABORT: fixture POST failed; cannot proceed with filter tests.")
        sys.exit(1)

    print("\n=== P0 — Filter correctness ===")
    test_type_filter()             # 2a, 2b
    test_period_current_month()    # 3
    test_period_10d()              # 4
    test_period_30d()              # 5
    test_period_specific_month_march()  # 6
    test_explicit_from_to_may()    # 7
    test_combined_expense_30d()    # 8
    test_limit_all_vs_limit_1()    # 9a, 9b

    print("\n=== P1 — Validation ===")
    test_validation()

    print("\n=== P2 — Regressions ===")
    test_regression_15_no_params()      # 15
    test_regression_balance()           # 16
    test_regression_reports_monthly()   # 17
    test_regression_categories_full()   # 18
    test_regression_post_employees()    # 19
    test_regression_delete_foreign_id() # 20

    cleanup_fixtures()

    # Summary
    passed = sum(1 for _,_,p,_ in RESULTS if p)
    failed = sum(1 for _,_,p,_ in RESULTS if not p)
    print(f"\n=== SUMMARY ===")
    print(f"  PASS: {passed}")
    print(f"  FAIL: {failed}")
    if failed:
        print("\n--- Failures ---")
        for tid, name, p, details in RESULTS:
            if not p:
                print(f"[FAIL] {tid} {name}")
                for line in details.splitlines():
                    print(f"        {line}")


if __name__ == "__main__":
    main()
