# Adapted from turing-smart-screen-python; see THIRD_PARTY.md.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit legacy New API token acquisition; never used by periodic polling."""

import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yaml


def create_management_token(settings, local_path):
    """Rotate once using a verified cookie session, save atomically, then verify."""
    path = Path(local_path)
    try:
        local = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, yaml.YAMLError):
        raise ValueError("Cannot read local settings; check permissions and YAML syntax.") from None
    if local is None:
        local = {}
    if not isinstance(local, dict):
        raise ValueError("Local settings must contain a YAML mapping.")
    target = local
    if "dashboard" in local:
        target = local["dashboard"]
        if not isinstance(target, dict):
            raise ValueError("dashboard must contain a YAML mapping.")
    url = str(os.environ.get("NEW_API_URL") or settings.get("NEW_API_URL", "")).strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Set NEW_API_URL to the website base URL.")
    cookie = str(os.environ.get("NEW_API_COOKIE") or settings.get("NEW_API_COOKIE", "")).strip()
    user_id = str(os.environ.get(settings.get("NEW_API_USER_ID_ENV", "NEW_API_USER_ID")) or
                  settings.get("NEW_API_USER_ID", "")).strip()
    if not cookie or not user_id:
        raise ValueError("Set NEW_API_COOKIE and NEW_API_USER_ID from your logged-in browser first.")
    headers = {"Cookie": cookie, "New-Api-User": user_id}
    timeout = float(settings.get("TIMEOUT_SECONDS", 5))

    def request(endpoint, request_headers, rotation=False):
        try:
            response = requests.get(url + endpoint, headers=request_headers, timeout=timeout,
                                    allow_redirects=False)
            if response.status_code != 200:
                raise ValueError(f"New API HTTP {response.status_code}. Token generation was not retried.")
            body = response.json()
            if not isinstance(body, dict) or body.get("success") is not True:
                raise ValueError("New API rejected the request. Token generation was not retried.")
            return body.get("data")
        except requests.exceptions.JSONDecodeError:
            raise ValueError("New API returned an unreadable response; check the website before retrying.") from None
        except requests.RequestException:
            message = ("Token request failed; the server may already have rotated the token. "
                       "Check the site's security settings before retrying." if rotation else
                       "Account verification request failed.")
            raise ValueError(message) from None

    # Validate the selected browser account before invoking the rotating endpoint.
    account = request("/api/user/self", headers)
    if not isinstance(account, dict) or str(account.get("id")) != user_id:
        raise ValueError("Browser session account does not match NEW_API_USER_ID; no token generated.")
    token = request("/api/user/token", headers, rotation=True)
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Token response is invalid; check the site's security settings before retrying.")
    token = token.strip()
    target.update(NEW_API_URL=url, NEW_API_ACCESS_TOKEN=token, NEW_API_AUTH_MODE="token",
                 NEW_API_USER_ID=user_id, DEMO=False)
    # Save before verification so a transient failure cannot discard the new token.
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".new-api-", delete=False) as stream:
            temporary = Path(stream.name)
            yaml.safe_dump(local, stream, sort_keys=False)
        os.replace(temporary, path)
    except OSError:
        raise ValueError("Token generated but could not be saved. Recover it in the site's security settings.") from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    try:
        verified = request("/api/user/self", {"Authorization": "Bearer " + token, "New-Api-User": user_id})
        if not isinstance(verified, dict) or str(verified.get("id")) != user_id:
            raise ValueError("Token account identity mismatch.")
    except ValueError:
        raise ValueError("New token saved locally, but verification failed. Run check-new-api.py without --create-token.") from None
