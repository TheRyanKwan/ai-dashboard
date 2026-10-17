# SPDX-License-Identifier: GPL-3.0-or-later
"""Check configured account access without printing credentials or balances."""

import argparse
from pathlib import Path

from ._vendor.turing.ai_dashboard import AccountCache
from ._vendor.turing.new_api_setup import create_management_token
from .config import load_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("dashboard.local.yaml"),
                        help="configuration to read and update (default: dashboard.local.yaml)")
    parser.add_argument("--create-token", action="store_true",
                        help="Call GET /api/user/token once using your cookie; "
                             "may invalidate the old token")
    args = parser.parse_args(argv)
    path = args.config.expanduser()
    try:
        settings = dict(load_settings(path if path.exists() else None).dashboard, DEMO=False)
        if not path.exists() and path != Path("dashboard.local.yaml"):
            raise ValueError("Configuration file does not exist.")
        if args.create_token:
            print("Calling the legacy token endpoint once; "
                  "the previous management token may be invalidated.")
            create_management_token(settings, path)
            print("Management token saved and verified. Restart the dashboard.")
            print("Previously exported NEW_API_ACCESS_TOKEN or NEW_API_USER_ID values "
                  "override the saved file; unset them.")
            return 0
        cache = AccountCache()
        cache.refresh(settings)
        print("Auth mode: " + str(settings.get("NEW_API_AUTH_MODE", "token")))
        print("New API: " + (cache.error or "Connected; account fields validated"))
        if cache.error:
            if str(settings.get("NEW_API_AUTH_MODE", "token")).lower() == "cookie":
                print("Check the browser session Cookie header and matching account user ID; "
                      "sessions expire.")
            else:
                print("Use a management access token, not an inference key or "
                      "a temporary dashboard login token.")
                print("Alternatively use NEW_API_AUTH_MODE: cookie with your browser "
                      "session's Cookie header.")
            return 1
        return 0
    except (OSError, ValueError) as error:
        # File errors can include paths; YAML errors are redacted by load_settings.
        print(str(error) if isinstance(error, ValueError) else "Cannot access configuration file.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
