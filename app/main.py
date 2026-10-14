import asyncio
import hashlib
import json
import os
import secrets
import shutil
import signal
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mutagen import File as AudioFile
from pydantic import BaseModel, Field, field_validator

from app.playlists import MAX_IMPORT_BYTES, MAX_TRACKS, parse_csv_playlist, parse_spotify_preview, spotify_playlist_id

DATA = Path(os.getenv("DATA_DIR", "/data"))
LIBRARY = Path(os.getenv("LIBRARY_DIR", "/library"))
DOWNLOADS = Path(os.getenv("SLSKD_DOWNLOADS_DIR", "/downloads"))
STATIC = Path(__file__).parent / "static"


class DownloadCancelled(Exception):
    pass


def connect():
    db = sqlite3.connect(DATA / "jobs.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    return db


def update(job_id, **values):
    values.setdefault("last_update", time.time())
    with connect() as db:
        db.execute(f"UPDATE jobs SET {', '.join(k + '=?' for k in values)} WHERE id=?",
                   [*values.values(), job_id])


def check_cancelled(job_id):
    with connect() as db:
        row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row and row["status"] in {"Cancelling", "Cancelled"}:
        raise DownloadCancelled()


def record_progress(job_id, downloaded, total=None, speed=None, eta=None, transfer_state=None, queue_position=None):
    now = time.time()
    with connect() as db:
        previous = db.execute("SELECT downloaded_bytes,total_bytes,progress,last_activity FROM jobs WHERE id=?", (job_id,)).fetchone()
    downloaded = max(0, int(downloaded or 0))
    total = int(total or previous["total_bytes"] or 0) or None
    moved = downloaded != previous["downloaded_bytes"]
    update(job_id, downloaded_bytes=downloaded, total_bytes=total,
           progress=min(100, downloaded / total * 100) if total else previous["progress"],
           speed=max(0, speed or 0) if moved else 0, eta=eta if moved else None,
           transfer_state=transfer_state, queue_position=queue_position, last_update=now,
           last_activity=now if moved else previous["last_activity"] or now)


async def command(*args, timeout=120, progress_job=None):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL if progress_job else asyncio.subprocess.PIPE,
                                                start_new_session=True)
    cancelled = False

    async def watch_cancellation():
        nonlocal cancelled
        while proc.returncode is None:
            try:
                check_cancelled(progress_job)
            except DownloadCancelled:
                cancelled = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(watch_cancellation()) if progress_job else None

    async def output():
        if not progress_job:
            return await proc.communicate()
        while line := await proc.stdout.readline():
            if not line.startswith(b"hmr:"):
                continue
            try:
                progress = json.loads(line[4:])
            except (ValueError, UnicodeDecodeError):
                continue
            record_progress(progress_job, progress.get("downloaded_bytes"),
                            total=progress.get("total_bytes") or progress.get("total_bytes_estimate"),
                            speed=progress.get("speed"), eta=progress.get("eta"),
                            transfer_state="Download finished" if progress.get("status") == "finished" else "Downloading audio")
        await proc.wait()
        return b"", b""

    try:
        stdout, stderr = await asyncio.wait_for(output(), timeout)
    except BaseException:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await proc.wait()
        raise
    finally:
        if watcher:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
    if cancelled:
        raise DownloadCancelled()
    if proc.returncode:
        # Do not expose upstream URLs, credentials, or local paths to the browser.
        raise RuntimeError(f"{Path(args[0]).name} failed; check service connectivity and source availability")
    return stdout


async def slsk(method, path, **kwargs):
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(method, os.environ["SLSKD_URL"].rstrip("/") + "/api/v0" + path,
                                        headers={"X-API-Key": os.environ["SLSKD_API_KEY"]}, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None


async def navidrome(method, **params):
    salt = secrets.token_hex(16)
    token = hashlib.md5((os.environ["NAVIDROME_PASSWORD"] + salt).encode()).hexdigest()
    auth = dict(u=os.environ["NAVIDROME_USER"], t=token, s=salt, v="1.16.1", c="sado", f="json")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(os.environ["NAVIDROME_URL"].rstrip("/") + f"/rest/{method}.view",
                                    params={**auth, **params})
        response.raise_for_status()
        payload = response.json()["subsonic-response"]
        if payload["status"] != "ok":
            raise RuntimeError("Navidrome rejected the request; check credentials and admin permissions")
        return payload


async def search_soulseek(query):
    # slskd passes this value to Soulseek.NET in milliseconds, not seconds.
    search = await slsk("POST", "/searches", json={"searchText": query, "searchTimeout": 15000,
                                                 "fileLimit": 500, "responseLimit": 50})
    search_id = search["id"]
    for _ in range(35):
        await asyncio.sleep(1)
        search = await slsk("GET", f"/searches/{search_id}")
        if search.get("isComplete"):
            break
    responses = await slsk("GET", f"/searches/{search_id}/responses")
    results = []
    for peer in responses:
        for file in peer.get("files", []):
            extension = file["filename"].rsplit(".", 1)[-1].lower()
            if extension not in {"flac", "mp3"} or file.get("isLocked"):
                continue
            results.append(dict(source="soulseek", title=file["filename"].replace("\\", "/").split("/")[-1],
                                filename=file["filename"], username=peer["username"], size=file["size"],
                                format=extension, bitrate=file.get("bitRate"), duration=file.get("length"),
                                bit_depth=file.get("bitDepth"), sample_rate=file.get("sampleRate"),
                                queue=peer.get("queueLength"), free_slot=peer.get("hasFreeUploadSlot"),
                                search_id=search_id))
            if len(results) >= 200:
                return results
    return results


async def search_youtube(query):
    raw = await command("yt-dlp", "--ignore-config", "--js-runtimes", "node", "--flat-playlist", "--dump-single-json",
                        "--no-warnings", "--", f"ytsearch12:{query}", timeout=60)
    payload = json.loads(raw)
    return [dict(source="youtube", title=e.get("title", "Untitled"), video_id=e["id"],
                 channel=e.get("channel") or e.get("uploader"), duration=e.get("duration"),
                 thumbnail=f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg", format="m4a")
            for e in payload.get("entries", []) if e and e.get("id")]


def tag_youtube_audio(source, video_title):
    audio = AudioFile(source, easy=True)
    if audio is None:
        raise RuntimeError("Downloaded file is not valid audio")
    if audio.tags is None:
        audio.add_tags()
    # Never treat the upload channel as the recording artist. Only infer an
    # artist from the explicit Artist - Song convention, preserving existing tags.
    first_title = video_title.split("|", 1)[0].strip()
    artist, separator, title = first_title.partition(" - ")
    if separator and artist.strip() and title.strip():
        if not audio.get("artist"):
            audio["artist"] = [artist.strip()]
        if not audio.get("title"):
            audio["title"] = [title.strip()]
    elif not audio.get("title"):
        audio["title"] = [video_title.strip()]
    audio.save()


def import_audio(source, job_id):
    metadata = AudioFile(source)
    if metadata is None or not getattr(metadata.info, "length", 0):
        raise RuntimeError("Downloaded file is not valid audio")
    destination_dir = LIBRARY / "Home Music Request"
    destination_dir.mkdir(parents=True, exist_ok=True)
    # Copy into the target filesystem first, then publish atomically. Never overwrite a track.
    name = source.name.replace("/", "_").replace("\\", "_")
    destination = destination_dir / f"{job_id[:8]}-{name}"
    temporary = destination_dir / f".{job_id}.part"
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        source.unlink()
    finally:
        temporary.unlink(missing_ok=True)
    return str(destination.relative_to(LIBRARY))


async def download(job):
    job_id = job["id"]
    selection = json.loads(job["selection"])
    check_cancelled(job_id)
    update(job_id, status="Downloading", last_activity=time.time(), transfer_state="Connecting to source")
    if selection["source"] == "soulseek":
        destination = f"home-music-request/{job_id}"
        batch = await slsk("POST", "/transfers/downloads/batches", json={
            "id": job_id, "username": selection["username"], "searchId": selection["search_id"],
            "files": [{"filename": selection["filename"], "size": selection["size"]}],
            "options": {"destination": destination, "externalId": job_id}})
        if batch.get("failures") or not batch["batch"].get("transfers"):
            raise RuntimeError("slskd could not enqueue this file")
        check_cancelled(job_id)
        deadline = time.monotonic() + int(os.getenv("DOWNLOAD_TIMEOUT", "3600"))
        previous_bytes = 0
        previous_time = time.monotonic()
        while time.monotonic() < deadline:
            check_cancelled(job_id)
            batch = await slsk("GET", f"/transfers/downloads/batches/{job_id}")
            check_cancelled(job_id)
            transfer = batch["transfers"][0]
            downloaded = transfer.get("bytesTransferred", 0)
            total = transfer.get("size") or selection["size"]
            now = time.monotonic()
            speed = max(0, downloaded - previous_bytes) / max(now - previous_time, 0.001)
            record_progress(job_id, downloaded, total=total, speed=speed,
                            eta=max(0, total - downloaded) / speed if speed else None,
                            transfer_state=transfer["state"], queue_position=transfer.get("placeInQueue"))
            previous_bytes, previous_time = downloaded, now
            flags = {s.strip() for s in transfer["state"].split(",")}
            if "Completed" in flags:
                if "Succeeded" not in flags:
                    raise RuntimeError("Soulseek transfer failed; select another peer")
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError("Soulseek transfer timed out; it may still be queued in slskd")
        folder = DOWNLOADS / destination
        update(job_id, transfer_state="Waiting for slskd to publish completed file", speed=0, eta=None)
        # slskd persists success just before moving the file into its final directory.
        for _ in range(30):
            check_cancelled(job_id)
            files = list(folder.glob("*")) if folder.exists() else []
            files = [p for p in files if p.is_file() and not p.is_symlink() and p.suffix.lower() in {".flac", ".mp3"}]
            if len(files) == 1 and files[0].stat().st_size == selection["size"]:
                source = files[0]
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Completed file not found in the shared per-job download directory")
    else:
        folder = DATA / "staging" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        await command("yt-dlp", "--ignore-config", "--js-runtimes", "node", "--no-playlist", "--no-warnings",
                      "--quiet", "--progress", "--newline", "--progress-delta", "1",
                      "--progress-template", "download:hmr:%(progress)j",
                      "-f", "bestaudio[ext=m4a]/bestaudio", "-o", str(folder / "%(title).120B.%(ext)s"),
                      "--", f"https://www.youtube.com/watch?v={selection['video_id']}",
                      timeout=int(os.getenv("DOWNLOAD_TIMEOUT", "3600")), progress_job=job_id)
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix in {".m4a", ".webm", ".opus", ".mp3", ".ogg"}]
        if len(files) != 1:
            raise RuntimeError("Expected a single downloaded audio file")
        source = files[0]
    # Commit the processing boundary atomically with respect to cancellation.
    # Once processing starts, cancellation is rejected rather than racing a file import.
    with connect() as db:
        changed = db.execute("UPDATE jobs SET status='Processing',progress=100,speed=0,eta=NULL,transfer_state='Preparing audio',last_activity=?,last_update=? WHERE id=? AND status='Downloading'",
                             (time.time(), time.time(), job_id)).rowcount
    if not changed:
        raise DownloadCancelled()
    if source.suffix == ".webm":
        output = source.with_suffix(".opus")
        await command("ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-vn", "-c:a", "copy", str(output))
        source.unlink()
        source = output
    if selection["source"] == "youtube":
        await asyncio.to_thread(tag_youtube_audio, source, selection["title"])
    update(job_id, transfer_state="Importing into library")
    path = await asyncio.to_thread(import_audio, source, job_id)
    update(job_id, path=path, transfer_state="Requesting Navidrome scan")
    try:
        await navidrome("startScan")
    except Exception:
        update(job_id, warning="Track imported, but Navidrome scan failed. Use Scan Again after checking admin credentials.")
    update(job_id, status="Complete", progress=100, transfer_state="Imported into library")


async def cancel_download(job):
    job_id = job["id"]
    with connect() as db:
        if db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "Cancelled":
            return True
    selection = json.loads(job["selection"])
    try:
        if selection["source"] == "soulseek":
            batch = await slsk("GET", f"/transfers/downloads/batches/{job_id}")
            if not batch.get("transfers"):
                # An enqueue request may still be running in slskd after an HTTP
                # timeout. An empty batch is not proof that no transfer will start.
                raise RuntimeError("Waiting for slskd to expose the transfer")
            for transfer in batch.get("transfers", []):
                await slsk("DELETE", f"/transfers/downloads/{quote(selection['username'], safe='')}/{transfer['id']}")
            for _ in range(10):
                batch = await slsk("GET", f"/transfers/downloads/batches/{job_id}")
                states = [{flag.strip() for flag in transfer["state"].split(",")} for transfer in batch.get("transfers", [])]
                if all("Completed" in state and bool(state & {"Cancelled", "Succeeded"}) for state in states):
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Cancellation not yet confirmed")
        else:
            # The subprocess has already exited before the worker reaches here.
            await asyncio.to_thread(shutil.rmtree, DATA / "staging" / job_id, True)
        update(job_id, status="Cancelled", speed=0, eta=None, error=None, transfer_state="Cancelled by user")
        return True
    except Exception:
        update(job_id, error="Could not confirm cancellation. Retrying; check slskd connectivity if this persists.",
               transfer_state="Cancellation pending", speed=0, eta=None)
        return False


async def worker():
    while True:
        with connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE status IN ('Queued','Cancelling') ORDER BY created LIMIT 1").fetchone()
        if job:
            try:
                check_cancelled(job["id"])
                await download(job)
            except DownloadCancelled:
                if not await cancel_download(job):
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                try:
                    check_cancelled(job["id"])
                except DownloadCancelled:
                    if not await cancel_download(job):
                        await asyncio.sleep(5)
                else:
                    message = str(exc) if isinstance(exc, RuntimeError) else "Download failed; check service configuration and connectivity"
                    update(job["id"], status="Failed", error=message, speed=0, eta=None)
        else:
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    DATA.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript("""
          CREATE TABLE IF NOT EXISTS selections (id TEXT PRIMARY KEY, payload TEXT, created REAL);
          CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, selection_id TEXT UNIQUE, selection TEXT,
            title TEXT, source TEXT, status TEXT, progress REAL DEFAULT 0, created REAL,
            path TEXT, error TEXT, warning TEXT);
        """)
        # Existing deployments already have job history; add metrics in place.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
        for name, definition in {
            "downloaded_bytes": "INTEGER NOT NULL DEFAULT 0", "total_bytes": "INTEGER",
            "speed": "REAL", "eta": "REAL", "transfer_state": "TEXT", "queue_position": "INTEGER",
            "last_activity": "REAL", "last_update": "REAL",
        }.items():
            if name not in columns:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        db.execute("UPDATE jobs SET status='Failed', error='Service restarted during download; check slskd before retrying' WHERE status IN ('Downloading','Processing')")
    task = asyncio.create_task(worker())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Sado", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def protect_mutations(request: Request, call_next):
    # A custom header prevents cross-site forms from submitting download/scan requests.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and (
        request.headers.get("X-Requested-With") != "HomeMusicRequest"
        or request.headers.get("Sec-Fetch-Site") == "cross-site"
    ):
        return JSONResponse({"detail": "Same-origin API request required"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https://i.ytimg.com; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/search")
async def search(q: str = Query(min_length=2, max_length=200)):
    async def local():
        data = await navidrome("search3", query=q, artistCount=0, albumCount=0, songCount=30)
        return data.get("searchResult3", {}).get("song", [])

    values = await asyncio.gather(local(), search_soulseek(q), search_youtube(q), return_exceptions=True)
    response = {"local": [], "results": [], "errors": {}}
    with connect() as db:
        db.execute("DELETE FROM selections WHERE created < ?", (time.time() - 86400,))
        for name, value in zip(("navidrome", "soulseek", "youtube"), values):
            if isinstance(value, BaseException):
                response["errors"][name] = "Source unavailable; check connection and credentials"
            elif name == "navidrome":
                response["local"] = value
            else:
                for item in value:
                    selection_id = str(uuid.uuid4())
                    db.execute("INSERT INTO selections VALUES (?, ?, ?)", (selection_id, json.dumps(item), time.time()))
                    response["results"].append({**item, "id": selection_id})
    return response


class DownloadRequest(BaseModel):
    selection_id: str
    permitted: bool = False


class BatchDownloadRequest(BaseModel):
    selection_ids: list[str] = Field(min_length=1, max_length=100)
    permitted: bool = False

    @field_validator("selection_ids")
    @classmethod
    def bounded_ids(cls, values):
        if any(not value or len(value) > 100 for value in values):
            raise ValueError("Selection IDs must be between 1 and 100 characters")
        return values


def enqueue_selections(selection_ids, permitted):
    if not permitted:
        raise HTTPException(400, "Confirm you have permission to download this audio")
    selection_ids = list(dict.fromkeys(selection_ids))
    with connect() as db:
        rows = {row["id"]: row for row in db.execute(
            f"SELECT * FROM selections WHERE id IN ({','.join('?' for _ in selection_ids)}) AND created>?",
            [*selection_ids, time.time() - 86400])}
        if len(rows) != len(selection_ids):
            raise HTTPException(404, "A search result expired or is unavailable. Find versions again; no new requests were queued.")
        jobs = []
        for selection_id in selection_ids:
            row = rows[selection_id]
            item = json.loads(row["payload"])
            existing = db.execute("SELECT id FROM jobs WHERE selection_id=?", (selection_id,)).fetchone()
            job_id = existing["id"] if existing else str(uuid.uuid4())
            if not existing:
                db.execute("INSERT INTO jobs (id,selection_id,selection,title,source,status,created) VALUES (?,?,?,?,?,'Queued',?)",
                           (job_id, selection_id, row["payload"], item["title"], item["source"], time.time()))
            jobs.append({"id": job_id, "selection_id": selection_id})
    return jobs


@app.post("/api/downloads", status_code=202)
async def enqueue(request: DownloadRequest):
    return {"id": enqueue_selections([request.selection_id], request.permitted)[0]["id"]}


@app.post("/api/downloads/batch", status_code=202)
async def enqueue_batch(request: BatchDownloadRequest):
    return {"jobs": enqueue_selections(request.selection_ids, request.permitted)}


class PlaylistTrack(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(min_length=1, max_length=200)
    album: str = Field(default="", max_length=300)
    duration: float | None = Field(default=None, ge=0, le=86400, allow_inf_nan=False)
    spotify_uri: str | None = Field(default=None, max_length=100)

    @field_validator("title", "artist", "album", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class PlaylistCheckRequest(BaseModel):
    tracks: list[PlaylistTrack] = Field(min_length=1, max_length=MAX_TRACKS)


class SpotifyImportRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2048)


@app.post("/api/playlists/spotify")
async def import_spotify(request: SpotifyImportRequest):
    try:
        playlist_id = spotify_playlist_id(request.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # Fetch only a canonical Spotify URL; never follow redirects or use caller-supplied hosts.
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            async with client.stream("GET", f"https://open.spotify.com/embed/playlist/{playlist_id}") as response:
                response.raise_for_status()
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_IMPORT_BYTES:
                        raise HTTPException(502, "Spotify preview is too large; upload an exported CSV instead")
                    chunks.append(chunk)
        return parse_spotify_preview(b"".join(chunks).decode("utf-8"), playlist_id)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(502, "Spotify preview unavailable or unreadable. Check that the playlist is public, or upload an exported CSV.") from exc
    except httpx.HTTPError:
        raise HTTPException(502, "Spotify preview could not be fetched. Try again or upload an exported CSV.")


@app.post("/api/playlists/csv")
async def import_csv(request: Request):
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_IMPORT_BYTES:
            raise HTTPException(413, "CSV must be at most 2 MB")
        chunks.append(chunk)
    try:
        text = b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV must use UTF-8 encoding")
    try:
        return await asyncio.to_thread(parse_csv_playlist, text)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/playlists/check")
async def check_playlist(request: PlaylistCheckRequest):
    semaphore = asyncio.Semaphore(4)
    unavailable = False

    async def check(track):
        nonlocal unavailable
        item = track.model_dump()
        item.update(local=[], library_error=None)
        async with semaphore:
            if not unavailable:
                try:
                    # Title-only lookup also finds untagged audio indexed under Unknown Artist.
                    data = await navidrome("search3", query=track.title, artistCount=0, albumCount=0, songCount=10)
                    item["local"] = data.get("searchResult3", {}).get("song", [])
                    return item
                except Exception:
                    unavailable = True
            item["library_error"] = "Library check unavailable. Retry after checking Navidrome connectivity and credentials."
        return item

    return {"tracks": await asyncio.gather(*(check(track) for track in request.tracks))}


@app.post("/api/downloads/{job_id}/cancel", status_code=202)
async def cancel(job_id: str):
    with connect() as db:
        job = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "Download not found")
        if job["status"] in {"Cancelling", "Cancelled"}:
            return {"id": job_id, "status": job["status"]}
        if job["status"] not in {"Queued", "Downloading"}:
            raise HTTPException(409, "This request can no longer be cancelled; processing may already have started")
        status = "Cancelled" if job["status"] == "Queued" else "Cancelling"
        db.execute("UPDATE jobs SET status=?,speed=0,eta=NULL,transfer_state=?,last_update=? WHERE id=?",
                   (status, "Cancelled by user" if status == "Cancelled" else "Stopping download", time.time(), job_id))
    return {"id": job_id, "status": status}


@app.get("/api/downloads")
async def jobs():
    with connect() as db:
        results = [dict(row) for row in db.execute("SELECT id,title,source,status,progress,created,path,error,warning,downloaded_bytes,total_bytes,speed,eta,transfer_state,queue_position,last_activity,last_update FROM jobs ORDER BY created DESC LIMIT 100")]
    for job in results:
        job["idle_seconds"] = max(0, int(time.time() - (job["last_activity"] or job["created"])))
        job["waiting_for_peer"] = job["status"] == "Downloading" and "Queued" in (job["transfer_state"] or "")
        job["stalled"] = job["status"] == "Downloading" and not job["waiting_for_peer"] and job["idle_seconds"] >= 60
    return results


@app.post("/api/scan")
async def scan():
    try:
        await navidrome("startScan")
    except Exception:
        raise HTTPException(502, "Scan failed; check Navidrome connectivity and admin credentials")
    return {"status": "requested"}
