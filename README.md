# Sado

A small music-request application for selecting individual recordings from Soulseek and YouTube and importing them into Navidrome. Search checks all three sources concurrently; it never automatically picks or downloads a recording.

**Access:** Sado has no login. Anyone who can reach the site can search, request downloads, cancel active jobs, and trigger library scans. HTTPS and cross-site request protections remain enabled, but do not restrict who can use the service. Use network/proxy access restrictions if it should only be available to your household. Upstream slskd and Navidrome credentials remain private on the server.

## Deployment

This Compose file is tailored to the existing services on this machine:

- slskd: `http://slskd:5030`, on the external `web` network.
- Navidrome: `http://navidrome:4533`, on the same network.
- Shared completed Soulseek downloads: `/home/odina/music/slskd/downloads`.
- Destination library: `/home/odina/music/library`.
- Container UID/GID: `1000:1000`, matching the existing services.

Create `.env` using `.env.example` as a template and supply your existing slskd API key and Navidrome **admin** credentials (required for scans). No application password is required; any old `APP_PASSWORD` entry is unused. `APP_DOMAIN` defaults to `sado.barnomasoz.com`; change it if your subdomain belongs to a different domain. Keep `.env` private. No existing service configuration needs to be changed.

Run from this directory:

```sh
docker compose up -d --build
docker compose logs -f sado
```

Open `https://sado.barnomasoz.com` (or your configured `APP_DOMAIN`); no login prompt is shown. Compose includes Traefik labels routing HTTPS traffic to `sado:8080` on the existing `web` network, using the existing `websecure` entrypoint and `le` certificate resolver. Point the subdomain's DNS A/AAAA records at this server before starting the service; DNS records are not created by the application. Traefik handles HTTPS and HTTP-to-HTTPS redirection.

Local access remains available at `http://localhost:8080`, bound to loopback only. The project directory, Docker data volume, and existing library/staging folder names remain unchanged so renaming the app does not disconnect stored job history or tracks. If you previously started the old `home-music-request` service, stop that container before starting `sado` to avoid a port conflict and two workers sharing one queue.

The application requires slskd **0.26.1 or newer** for the batch-download API. Its configured download directory must be `/downloads` inside slskd, sharing the host directory above. Keep its download subdirectory pattern at the default `${SOURCE_DIRECTORY}` or another normal pattern, not the special `"{}"` pattern, which ignores per-batch destinations. slskd must be connected to Soulseek and its API key must permit search and transfers. Both mounted host directories must be writable by UID 1000.

## Behavior

No separate YouTube Music API or Google API key is needed. YouTube search and authorized audio retrieval use yt-dlp; Node.js and the bundled challenge solver support YouTube extraction, and FFmpeg handles audio containers. This is not a YouTube Music catalog integration and does not provide account playlists or subscription access.

## Playlist Imports

Open **Import a Playlist** to paste an official Spotify playlist URL or upload a UTF-8 CSV. Importing retrieves **track names and artists only**, not Spotify audio, and does not search, skip, choose recordings, or start downloads automatically. No Spotify API key or account login is required for public previews. Spotify's embedded page may expose only part of a playlist, so every URL import is clearly labeled as a **potentially incomplete preview**. A private/unavailable playlist or a change to Spotify's page format may require CSV instead. Shortened Spotify links are not supported; use the full `https://open.spotify.com/playlist/...` link.

1. Import and review the track list.
2. Click **Check Library** for possible Navidrome matches. These are title-based candidates, not confirmed artist/recording duplicates. Unavailable checks show errors rather than reporting tracks as missing.
3. Click **Find Versions** for each track you want. Compare Soulseek formats/peers and YouTube titles/channels/durations. Refine the search within that playlist track if the original artist/title query is too specific.
4. Click **Choose This Version** to keep your exact choice for that row. This does not download it. Use **Skip**, **Undo Skip**, or **Remove Choice** to adjust the list.
5. Confirm permission for the selected recordings and click **Queue Selected**. Only your chosen, non-skipped recordings are queued, through the normal download/progress/cancel workflow. If any choice has expired, the entire batch is rejected without adding new jobs; find versions again and retry.

Imports and unqueued choices live only in the current browser page and disappear on reload. Queued jobs persist in SQLite. You can replace the imported list without deleting existing jobs. Source selections expire after 24 hours. Limits are **500 imported tracks**, **2 MiB per CSV/Spotify preview**, and **100 selections per batch**. Local-library checks use at most four concurrent requests and stop starting further lookups if Navidrome becomes unavailable. No source is chosen automatically, including for tracks with local matches.

CSV accepts comma, semicolon, or tab delimiters and quoted fields. Required headers are `Title` and `Artist`, or common export headers `Track Name` and `Artist Name(s)`. Optional headers include `Album` / `Album Name`, `Duration` (seconds or `mm:ss`/`hh:mm:ss`), `Track Duration (ms)`, and `Track URI`. Blank rows are ignored; invalid rows are rejected with an explanation rather than silently omitted. For example:

```csv
Title,Artist,Album,Duration
Thank You,Dido,No Angel,3:38
Seven Nation Army,The White Stripes,Elephant,3:52
```

Spotify URLs are reduced to a validated playlist ID and fetched only from a canonical Spotify hostname without following redirects. Only track metadata is returned; page/session tokens are not exposed to clients. CSV and result text is rendered as text, not HTML. These restrictions do not authenticate site users; the public-access warning above still applies.

## Download Behavior

- Soulseek search returns unlocked FLAC and MP3 files with peer, filename, size, bitrate, duration, bit depth, sample rate, and queue information when available. YouTube returns up to 12 videos, with title, channel, duration, thumbnail, and a preview link.
- Soulseek searches allow 15 seconds of inactivity for peer responses (`searchTimeout=15000`, in milliseconds), and Sado polls for up to 35 iterations before returning the available results. Search by artist or song title rather than a full peer folder path; browsing a specific peer's folders is not part of this interface.
- Local results are **possible matches**, not a recording-level duplicate guarantee. Search compares text through Navidrome's `search3` API. Review artist, album, and version yourself.
- A download requires an explicit permission confirmation. Only download audio you own or are authorized to retrieve. YouTube availability also depends on regional restrictions and yt-dlp support; the app does not bypass DRM or access controls.
- Search results remain selectable for 24 hours. The backend stores selected source details itself, rather than accepting arbitrary download URLs or filesystem paths from the browser.
- Jobs run serially through `Queued`, `Downloading`, `Processing`, and `Complete`; errors become `Failed`. Both sources show live percentage (when size is known), transferred/total size, transfer speed, estimated time remaining when available, and time since the last byte movement. Soulseek shows peer queue state/position; YouTube streams structured yt-dlp progress once per second. The UI refreshes every 2.5 seconds while visible.
- After 60 seconds without byte movement, an active transfer is marked **possibly stalled**, not failed or automatically restarted. Soulseek peer queues are labeled as waiting instead; a separate warning indicates when the backend has not refreshed transfer status for 20 seconds. Processing has its own phase labels, so 100% downloaded does not imply the track is already imported. Existing job history is preserved through an automatic SQLite schema update.
- **Cancel** is available for queued and downloading tracks. Queued requests cancel immediately; active transfers go through `Cancelling` to `Cancelled`. YouTube cancellation kills yt-dlp and its child processes and removes its per-job staging files. Soulseek cancellation targets only the transfers in that request's batch, confirms they have stopped, and leaves any slskd-managed incomplete files for slskd to handle. Cancellation errors remain visible and are retried rather than being reported as success; cancellation requests survive restarts. A pending cancellation can hold up later requests in the serial queue until the upstream transfer is confirmed stopped. Cancellation is not available after processing begins, and never removes an existing library track. Search again to make a new request after cancellation.
- FLAC and MP3 from Soulseek are imported unchanged. YouTube uses best available M4A where possible, otherwise its best audio stream. WebM/Opus is remuxed to Opus using FFmpeg stream copy, without lossy re-encoding or fake lossless conversion. YouTube imports receive title tags; when the video title uses `Artist - Song`, artist and title are inferred from the first segment before `|`. Existing artist/title tags are preserved. This convention is not an authoritative metadata lookup; other title formats retain the full video title and may still have an unknown artist. Upload channels are never used as artist names.
- Each Soulseek request uses a unique batch destination. Completed files are validated as audio, copied to a hidden temporary file in the library, atomically published in `Home Music Request/`, then removed from staging. A job ID prefix prevents naming collisions. The Navidrome scan is asynchronous.
- A scan failure does **not** undo a successful import. The job shows a warning; use **Scan Again** after correcting connectivity or admin credentials.
- SQLite and YouTube staging persist in the `request-data` Docker volume. Queued jobs survive restarts. Interrupted active jobs are marked failed rather than silently re-downloaded. Check slskd and the library before searching again to retry. slskd may continue a transfer after an application timeout or shutdown unless you explicitly cancelled it; automatic recovery/import of orphaned files is not implemented.
- Use exactly one Uvicorn worker and one application container. The SQLite queue is intentionally designed for this lightweight single-instance deployment.
- Results are bounded (200 Soulseek files, 12 YouTube videos, 30 local matches), and job history shows the latest 100 requests. Job records and abandoned staging files are not automatically purged; review disk usage periodically.

## Development

Python 3.12+, FFmpeg, Node.js 20+ (for YouTube's JavaScript challenges), and yt-dlp are required. The Docker image includes these dependencies and yt-dlp's bundled challenge solver. For non-Docker development, override `DATA_DIR`, `LIBRARY_DIR`, and `SLSKD_DOWNLOADS_DIR` with writable directories, and set the source credentials listed above. `SLSKD_URL` and `NAVIDROME_URL` must point to reachable services.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
```

The health check confirms the HTTP server and SQLite are usable. Source outages appear independently in search results; health does not imply all upstream services are available. Rebuild periodically to pick up current yt-dlp releases.
