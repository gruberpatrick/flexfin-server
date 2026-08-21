# flexfin-server

The server half of Flexfin.
A Flask app that speaks the Jellyfin API and serves Tidal as the library.

Point any Jellyfin client at it and you can browse, search and play Tidal without the client knowing Tidal exists.
Tested with Finamp and Feishin, and the intended companion is the Flexfin client.

Login is delegated to a real Jellyfin instance, so the server holds no user accounts and no passwords of its own.

## How it works

Flexfin translates Jellyfin API calls into [tidalapi](https://github.com/tamland/python-tidal) calls and renders the results as Jellyfin JSON.
Item ids are Tidal ids with a type prefix: `tidal_track_`, `tidal_album_`, `tidal_artist_`, `tidal_playlist_`.

Audio never passes through the server.
Playback returns an HLS playlist whose segments are Tidal CDN URLs, and downloads return a 302 to a single Tidal CDN URL, so the client fetches bytes directly from Tidal.
It only ever carries JSON and playlists.

A sqlite database caches every item the server surfaces, which feeds the image endpoint and per-track user data, and stores favourites, play counts and sessions.

## Requirements

- Python 3.10 or newer
- A Tidal subscription
- A reachable Jellyfin instance, used only to verify logins

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
export JELLYFIN_URL=http://your-jellyfin:8096
.venv/bin/python jellyfin_proxy.py                        # dev server on :8096
.venv/bin/gunicorn -b 0.0.0.0:8096 jellyfin_proxy:app     # or under gunicorn
```

Now point your client at Flexfin and log in with your **Jellyfin** username and password.
It forwards them to `JELLYFIN_URL` and, if Jellyfin accepts, issues its own token.

## Configuration

Everything is environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `JELLYFIN_URL` | none | Jellyfin base URL used to verify logins. Unset means every login is refused. |
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
The plan is to integrate with that same authenticated session and serve a single merged library, resolving each item to whichever source actually has it.

- Read the real Jellyfin library through the authenticated session and merge it with Tidal results, so one client sees both.
- Resolve each item to a source at request time: a local copy when one exists, Tidal otherwise.
- Prefer local files for downloads, which is what closes the quality gap described below, since a local copy can be served with `send_file` and full range support.

Two properties matter when that lands.
Source selection belongs in one function shared by the download, `PlaybackInfo` and HLS paths, because splitting it produces the case where downloads come from a local file while playback still streams from Tidal.
And the chosen source must never change an item's id or `MediaSource.Id`, or favourites, play counts and existing offline downloads are invalidated the moment a local copy appears.

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

**Session tokens are stored in plaintext** in the sqlite database.

## Exposing it

The token gate covers every route except `/System/Info`, `/System/Ping`, `/Users/AuthenticateByName` and the image endpoints.
It fails closed when `JELLYFIN_URL` is unset, when Jellyfin is unreachable, and when a response cannot be parsed.
Tokens are redacted from logs, because clients pass them in the query string and both werkzeug and gunicorn log the full request line.

That is not enough on its own to put on the public internet.
Login proxies straight through to your real Jellyfin, and Jellyfin does not lock out administrator accounts by default, so the limiter above is the only thing slowing password guessing.
Put it behind a tunnel with an access policy in front, or fix the limiter first.
Logging also defaults to `DEBUG`, which is more volume and more disclosure than a public deployment wants.
