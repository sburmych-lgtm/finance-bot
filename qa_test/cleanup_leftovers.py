"""Permanently delete explicitly named synthetic Ruby Finance QA accounts."""

from __future__ import annotations

import os
import sys
from typing import Mapping, Sequence

import requests

try:
    from qa_test.qa_runner import (
        ACCOUNT_DELETE_CONFIRMATION,
        SYNTHETIC_PREFIX,
        QaConfigError,
        _configure_utf8_console,
        _validate_base_url,
        make_init_data,
    )
except ModuleNotFoundError:  # direct: python qa_test/cleanup_leftovers.py ...
    from qa_runner import (  # type: ignore
        ACCOUNT_DELETE_CONFIRMATION,
        SYNTHETIC_PREFIX,
        QaConfigError,
        _configure_utf8_console,
        _validate_base_url,
        make_init_data,
    )


def parse_user_ids(
    arguments: Sequence[str], env: Mapping[str, str] | None = None
) -> tuple[int, ...]:
    source = os.environ if env is None else env
    raw_values = list(arguments)
    if not raw_values:
        combined = str(source.get("QA_USER_IDS", "")).strip()
        raw_values = [item.strip() for item in combined.split(",") if item.strip()]
    if not raw_values:
        raw_values = [
            str(source.get(name, "")).strip()
            for name in ("QA_USER_ID_A", "QA_USER_ID_B")
            if str(source.get(name, "")).strip()
        ]
    if not raw_values:
        raise QaConfigError(
            "provide synthetic ids as arguments or QA_USER_IDS/QA_USER_ID_A/B"
        )
    try:
        user_ids = tuple(dict.fromkeys(int(value) for value in raw_values))
    except ValueError as exc:
        raise QaConfigError("QA user ids must be integers") from exc
    if any(not str(user_id).startswith(SYNTHETIC_PREFIX) for user_id in user_ids):
        raise QaConfigError(f"cleanup ids must start with {SYNTHETIC_PREFIX}")
    return user_ids


def cleanup_account(
    base_url: str,
    token: str,
    user_id: int,
    *,
    timeout: float = 30.0,
) -> int:
    user = {
        "id": user_id,
        "first_name": "Ruby QA cleanup",
        "username": f"ruby_qa_{str(user_id)[-8:]}",
        "language_code": "uk",
    }
    response = requests.delete(
        f"{base_url}/api/account",
        headers={
            "Accept": "application/json",
            "X-Telegram-Init-Data": make_init_data(user, token),
        },
        json={"confirmation": ACCOUNT_DELETE_CONFIRMATION},
        timeout=timeout,
    )
    return response.status_code


def main(arguments: Sequence[str] | None = None) -> int:
    _configure_utf8_console()
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    try:
        user_ids = parse_user_ids(list(arguments if arguments is not None else sys.argv[1:]))
        base_url = _validate_base_url(os.environ.get("API_BASE_URL", ""))
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise QaConfigError("TELEGRAM_BOT_TOKEN is required")
        failed = []
        for user_id in user_ids:
            try:
                status = cleanup_account(base_url, token, user_id)
            except requests.RequestException as exc:
                print(f"{user_id}: transport failure ({type(exc).__name__})")
                failed.append(user_id)
                continue
            print(f"{user_id}: DELETE /api/account -> {status}")
            if status not in {200, 404, 410}:
                failed.append(user_id)
        return 1 if failed else 0
    except QaConfigError as exc:
        print(f"Cleanup configuration error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
