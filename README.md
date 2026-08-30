<img src="assets/flexfin-logo.svg" alt="" width="104">

# flexfin-server

The server half of Flexfin.
A Flask app that speaks the Jellyfin API and serves a merged library: the user's real Jellyfin library plus Tidal.

Point any Jellyfin client at it and you can browse, search and play both, without the client knowing Tidal exists.
Tested with Finamp and Feishin, and the intended companion is the Flexfin client.

Login is delegated to a real Jellyfin instance, so the server holds no user accounts and no passwords of its own.

## How it works

Flexfin translates Jellyfin API calls into [tidalapi](https://github.com/tamland/python-tidal) calls and renders the results as Jellyfin JSON.
Tidal item ids carry a type prefix: `tidal_track_`, `tidal_album_`, `tidal_artist_`, `tidal_playlist_`. Real Jellyfin items keep their native GUIDs, untouched.

Browse and search go through `merge.py`, which asks both sources and folds the results together.
The access token the real Jellyfin issues at login is kept (`sessions.jf_token`) and reused to read that user's library as them.
A Tidal item is dropped in favour of a Jellyfin one only when the two carry the same real identifier: ISRC for tracks, barcode/UPC for albums. No identifier, or no match, and both entries stay.

Audio and images never pass through the server.
A Tidal item redirects to Tidal's CDN; a local item redirects to the Jellyfin server itself (`JELLYFIN_PUBLIC_URL`). The process only ever carries JSON and playlists.

A sqlite database caches every Tidal item the server surfaces, which feeds the image endpoint and per-track user data, and stores favourites, play counts, sessions, and the Tidal↔Jellyfin identifier matches (`source_map`).

## Requirements

- Python 3.10 or newer
- A Tidal subscription
- A reachable Jellyfin instance, used to verify logins and to read the library

## Getting started

Install the dependencies:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Sign in to Tidal

Do this once, in a foreground terminal, **before** starting the server:

```bash
.venv/bin/python tidal_client.py
```

It prints a link and waits:

```
Visit https://link.tidal.com/XXXXX to log in, the code will expire in 300 seconds
```

Open that on any device and approve it.
The browser does not have to be on the same machine.
Credentials are then written to `tidal_creds.json` and refreshed automatically from then on, so this is a one time step.
Delete the file to sign in as a different Tidal account.

Do not rely on this happening by itself on the first request.
`login_oauth_simple()` blocks the thread that calls it, so under gunicorn the link goes into a worker log where nobody is looking and the request hangs until the worker times out.

### Run it

```bash
export JELLYFIN_URL=http://your-jellyfin:8096         # or put it in .env, see below
.venv/bin/python main.py                              # dev server on :8096
.venv/bin/gunicorn -b 0.0.0.0:8096 server:app        # or under gunicorn
```

Or via `make`, which creates the venv and installs requirements on first use:

```bash
make run                     # dev server on :8096
make serve                   # under gunicorn (HOST=/PORT= to override)
make tidal-login             # the one-time Tidal sign-in above
```

Now point your client at Flexfin and log in with your **Jellyfin** username and password.
It forwards them to `JELLYFIN_URL` and, if Jellyfin accepts, issues its own token.

## Configuration

Everything is environment variables. For local runs, copy `.env.example` to
`.env` and edit it — `server.py` (and `tidal_client.py`) load `.env` on startup.
Real environment variables override the file, and `.env` is gitignored.

| Variable | Default | Purpose |
| --- | --- | --- |
| `JELLYFIN_URL` | none | Jellyfin base URL, used to verify logins and to read the library. Unset means every login is refused. |
| `JELLYFIN_PUBLIC_URL` | `JELLYFIN_URL` | Base URL the *client* uses to fetch audio and images for local items. Set it when clients are not on the Jellyfin LAN and need a publicly reachable host; otherwise local-item playback and art only work for LAN clients. |
| `MERGE_LOCAL_LIBRARY` | `true` | Set to `false` to serve Tidal only, ignoring the Jellyfin library (the pre-merge behaviour). |
| `MERGE_TAG_SOURCE` | `false` | Testing aid: prefix every item name with `[J] ` or `[T] ` so it is visible in any client which source it came from. |
| `JELLYFIN_API_TIMEOUT` | `10` | Seconds to wait for a Jellyfin library call before falling back to Tidal-only results. |
| `PROXY_DB_PATH` | `./data/proxy.db` | sqlite cache, user data and sessions. |
| `LIKED_TRACKS_DIR` | `/mnt/Main/Apps/music_files` | Where marker files for liked items are written. |
| `TIDAL_DOWNLOAD_QUALITY` | `LOSSLESS` | Tier requested for downloads. See the note below. |
| `PROXY_TOKEN_TTL_DAYS` | `30` | Session lifetime. |
| `JELLYFIN_LOGIN_TIMEOUT` | `10` | Seconds to wait for Jellyfin during a login. |
| `PROXY_LOGIN_MAX_FAILURES` | `8` | Failed logins per window before lockout. |
| `PROXY_LOGIN_LOCKOUT_SECONDS` | `300` | Lockout window. |
| `TRUSTED_PROXY_IPS` | `127.0.0.1,::1` | Sources allowed to set `CF-Connecting-IP` / `X-Forwarded-For`. Set this if you run behind a tunnel or reverse proxy, or rate limiting will treat every remote client as one caller. |

## Useful endpoints

`GET /Proxy/Stats` reports the resolved database path, row counts and last write time per table, and a counter of dropped writes.
Cache writes are best effort so they never fail a request, which makes a broken database invisible otherwise.

## Liked items

Favouriting a track or album writes a marker file into `LIKED_TRACKS_DIR`:

```
track_55391454   containing   track:55391454
album_55391786   containing   album:55391786
```

The directory is append only, so un-favouriting leaves the marker in place.
This exists so an external process can watch the directory and fetch local copies of the things you like.

## Migrating from the pre-auth version

Before login was delegated to Jellyfin, every row was written under a placeholder user id.
Those rows are orphaned once real Jellyfin user ids arrive, so move them once:

```bash
python remap_user.py --list                    # see which ids own rows
python remap_user.py <your-jellyfin-user-id>   # move them
```

Get your user id from `GET /Users/Me` after logging in.

Every client also has to log in again, because the old build handed out a fixed fake token that is no longer valid.

## Roadmap

The point of authenticating against a real Jellyfin server is not just to check passwords.
Reading the real Jellyfin library through the authenticated session and merging it with Tidal results is now done (`merge.py`): one client sees both, and where a track or album carries the same ISRC or barcode on both sides the Tidal copy is dropped in favour of the local one.

What is left:

- **Fuzzy matching.** Dedupe only happens on an exact identifier match. A release with no ISRC/UPC on one side shows up twice.
- **Stable item ids across sources.** An item's id is simply its source's id today, so a Tidal favourite or play count goes stale the moment a local copy appears under a Jellyfin GUID. Items should resolve to a source at request time while keeping their id and `MediaSource.Id` fixed.
- **One shared source-selection function** for the download, `PlaybackInfo` and HLS paths, so a download can never come from a local file while playback still streams from Tidal (or vice versa).
- **Lossless downloads.** Serve local files with `send_file` and range support, which is what closes the quality gap in Known limitations below.
- **Sync favourites and play counts** to and from the real Jellyfin user data, instead of keeping them only in this server's sqlite.
- **Merge `/Genres` and `Similar`.** Both still return empty.

## Known limitations

**Downloads are lossy while playback is lossless.**
Tidal delivers `HI_RES_LOSSLESS` as multi segment DASH, which has no single URL to redirect to, so downloads ask for a tier served as one file.
On a typical account that tier comes back as AAC around 320 kbps.
Playback is unaffected and still gets 24 bit FLAC over HLS.
Serving lossless downloads means assembling the segments into a file, which is what the roadmap addresses via local copies.

**Image endpoints are unauthenticated.**
This matches Jellyfin, which marks them `[AllowAnonymous]` because browsers cannot attach headers to `<img>` tags, and clients such as Feishin rely on it.
Jellyfin gets away with it because its item ids are unguessable GUIDs.
Here the ids are Tidal's own sequential ids, so anyone who can reach the server can probe them.
Nothing private is exposed, only public Tidal artwork, but a prober can learn which items have been browsed through this server.

**`MediaSources` in item listings do not carry a token.**
`PlaybackInfo` does, which is the path Jellyfin documents, but a client that plays straight from a listing URL will get a 401.

**Rate limiting is per process and in memory.**
Under gunicorn with several workers the effective allowance is multiplied by the worker count, and it resets on restart.
It also has no per username counter, so an attacker with rotating addresses is not slowed.

**Session tokens are stored in plaintext** in the sqlite database. The Jellyfin access token (`sessions.jf_token`) is stored the same way.

**A session created before the merge landed has no Jellyfin token.**
Those requests get Tidal-only results until the client logs in again.

**Local items need `JELLYFIN_PUBLIC_URL` reachable by the client.**
Audio and images for local items are a 302 to the Jellyfin server. If a remote client cannot reach that URL it still *sees* local-only items in listings but cannot play them or load their art. Tidal items are unaffected.

**`TotalRecordCount` is approximate on merged responses.**
Each source is asked for up to `Limit` items and the merged list is sliced to a page, so paging past the first page of a large merged result is unreliable. This was already true of the Tidal-only paths.

**Artists are never deduplicated.**
Tidal exposes no cross-catalog identifier for an artist, so a Tidal artist and a Jellyfin artist for the same person show up as two entries.

## Exposing it

The token gate covers every route except `/System/Info`, `/System/Ping`, `/Users/AuthenticateByName` and the image endpoints.
It fails closed when `JELLYFIN_URL` is unset, when Jellyfin is unreachable, and when a response cannot be parsed.
Tokens are redacted from logs, because clients pass them in the query string and both werkzeug and gunicorn log the full request line.

That is not enough on its own to put on the public internet.
Login proxies straight through to your real Jellyfin, and Jellyfin does not lock out administrator accounts by default, so the limiter above is the only thing slowing password guessing.
Put it behind a tunnel with an access policy in front, or fix the limiter first.
Logging also defaults to `DEBUG`, which is more volume and more disclosure than a public deployment wants.
