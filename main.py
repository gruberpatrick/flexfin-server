"""Entrypoint for the Flexfin server.

Runs the Flask dev server after a couple of startup checks. Under gunicorn use
`gunicorn server:app` instead - every bit of wiring lives in server.py at import
time, so it runs the same either way; this file only adds the dev server and the
warnings below.
"""

import logging
import os

from server import app

log = logging.getLogger("flexfin")


def _preflight() -> None:
    if not os.environ.get("JELLYFIN_URL"):
        log.warning("JELLYFIN_URL is not set - every login will be refused and "
                    "the Jellyfin library cannot be read. See the README.")
    if not os.path.exists("tidal_creds.json"):
        log.warning("tidal_creds.json not found - run `python tidal_client.py` "
                    "once to sign in to Tidal before serving, or the first "
                    "request will hang on an interactive login.")


if __name__ == "__main__":
    _preflight()
    app.run(host="0.0.0.0", port=8096, debug=True)
