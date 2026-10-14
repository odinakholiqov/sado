import asyncio
import functools
import http.server
import json
import struct
import sys
import threading
import time
import uuid
import wave
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app import main

WORKER = main.worker

HEADERS = {"X-Requested-With": "HomeMusicRequest"}
SELECTION = {"source": "soulseek", "title": "Track.flac", "username": "peer",
             "filename": "Music\\Album\\Track.flac", "size": 100, "search_id": "search"}


def seed_job(selection=SELECTION, status="Queued"):
    job_id = str(uuid.uuid4())
    with main.connect() as db:
        db.execute("INSERT INTO jobs (id,selection_id,selection,title,source,status,created) VALUES (?,?,?,?,?,?,?)",
                   (job_id, job_id, json.dumps(selection), selection["title"], selection["source"], status, time.time()))
        return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def test_no_login_required(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "www-authenticate" not in response.headers
    assert "Choose Your Recording" in response.text
    assert "<title>Sado | Your Music Library</title>" in response.text
    assert main.app.title == "Sado"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/api/downloads").json() == []


def test_local_logo_and_favicon(client):
    page = client.get("/").text
    assert 'class="brand-logo" src="/static/winamp-logo.svg"' in page
    assert 'rel="icon" type="image/svg+xml" href="/static/winamp-logo.svg"' in page
    logo = client.get("/static/winamp-logo.svg")
    assert logo.status_code == 200
    assert "image/svg+xml" in logo.headers["content-type"]
    assert "<svg" in logo.text and "<script" not in logo.text
    credits = client.get("/static/logo-credits.txt")
    assert credits.status_code == 200 and "Emrrans" in credits.text and "Pduive23" in credits.text


def test_iphone_home_screen_icon(client):
    page = client.get("/").text
    assert 'rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png"' in page
    assert 'name="apple-mobile-web-app-title" content="Sado"' in page
    icon = client.get("/static/apple-touch-icon.png")
    assert icon.status_code == 200 and "image/png" in icon.headers["content-type"]
    assert icon.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", icon.content[16:24]) == (180, 180)


def test_csrf_protection(client):
    assert client.post("/api/scan").status_code == 403
    assert client.post("/api/scan", headers={**HEADERS, "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_partial_search_and_selection_validation(client, monkeypatch):
    monkeypatch.setattr(main, "navidrome", AsyncMock(return_value={"searchResult3": {"song": [{"title": "Track"}]}}))
    monkeypatch.setattr(main, "search_soulseek", AsyncMock(return_value=[SELECTION]))
    monkeypatch.setattr(main, "search_youtube", AsyncMock(side_effect=RuntimeError("private upstream detail")))
    response = client.get("/api/search", params={"q": "artist track"})
    assert response.status_code == 200
    data = response.json()
    assert data["local"] == [{"title": "Track"}]
    assert list(data["errors"]) == ["youtube"]
    assert "private" not in json.dumps(data["errors"])
    selection_id = data["results"][0]["id"]
    assert client.post("/api/downloads", headers=HEADERS, json={"selection_id": selection_id}).status_code == 400
    body = {"selection_id": selection_id, "permitted": True}
    queued = client.post("/api/downloads", headers=HEADERS, json=body)
    assert queued.status_code == 202
    assert client.post("/api/downloads", headers=HEADERS, json=body).json() == queued.json()
    jobs = client.get("/api/downloads").json()
    assert len(jobs) == 1 and jobs[0]["status"] == "Queued"
    assert "selection" not in jobs[0]
    assert client.post("/api/downloads", headers=HEADERS, json={"selection_id": "unknown", "permitted": True}).status_code == 404
    with main.connect() as db:
        db.execute("UPDATE selections SET created=0")
    assert client.post("/api/downloads", headers=HEADERS, json=body).status_code == 404


def test_empty_local_results_and_query_validation(client, monkeypatch):
    monkeypatch.setattr(main, "navidrome", AsyncMock(return_value={"searchResult3": {}}))
    monkeypatch.setattr(main, "search_soulseek", AsyncMock(return_value=[]))
    monkeypatch.setattr(main, "search_youtube", AsyncMock(return_value=[]))
    assert client.get("/api/search", params={"q": "song"}).json() == {"local": [], "results": [], "errors": {}}
    assert client.get("/api/search", params={"q": "x"}).status_code == 422


def test_searches_run_concurrently(client, monkeypatch):
    entered = set()
    ready = asyncio.Event()

    async def source(name, value):
        entered.add(name)
        if len(entered) == 3:
            ready.set()
        await asyncio.wait_for(ready.wait(), 1)
        return value

    async def local(*args, **kwargs):
        return await source("local", {"searchResult3": {}})

    async def soulseek(*args):
        return await source("soulseek", [])

    async def youtube(*args):
        return await source("youtube", [])

    monkeypatch.setattr(main, "navidrome", local)
    monkeypatch.setattr(main, "search_soulseek", soulseek)
    monkeypatch.setattr(main, "search_youtube", youtube)
    assert client.get("/api/search", params={"q": "song"}).json()["errors"] == {}
    assert len(entered) == 3


def test_restart_retains_queue_and_marks_active_failed(client):
    queued = seed_job()
    active = seed_job(status="Downloading")
    with TestClient(main.app) as second:
        second.auth = client.auth
        jobs = {j["id"]: j for j in second.get("/api/downloads").json()}
        assert jobs[queued["id"]]["status"] == "Queued"
        assert jobs[active["id"]]["status"] == "Failed"
        assert "restarted" in jobs[active["id"]]["error"]


def test_soulseek_search_filters_and_metadata(client, monkeypatch):
    slsk = AsyncMock(side_effect=[{"id": "search"}, {"isComplete": True}, [
        {"username": "peer", "queueLength": 2, "hasFreeUploadSlot": False, "files": [
            {"filename": "Album\\Track.FLAC", "size": 200, "bitDepth": 24, "sampleRate": 96000},
            {"filename": "Album\\locked.mp3", "size": 10, "isLocked": True},
            {"filename": "Album\\art.jpg", "size": 10}]}]])
    monkeypatch.setattr(main, "slsk", slsk)
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    results = asyncio.run(main.search_soulseek("song"))
    assert len(results) == 1
    assert results[0]["format"] == "flac" and results[0]["bit_depth"] == 24
    assert results[0]["sample_rate"] == 96000
    assert slsk.call_args_list[0].kwargs["json"]["searchTimeout"] == 15000


def test_soulseek_waits_for_delayed_peer_response(client, monkeypatch):
    filename = "music\\00_ALL_MUSIC\\Let Babylon Burn\\Singles\\Let Babylon Burn - Battleground of Broken Hearts.mp3"
    slsk = AsyncMock(side_effect=[
        {"id": "search"},
        *[{"isComplete": False} for _ in range(16)],
        {"isComplete": True},
        [{"username": "peer", "files": [{"filename": filename, "size": 15392574, "bitRate": 320}]}],
    ])
    monkeypatch.setattr(main, "slsk", slsk)
    sleep = AsyncMock()
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    results = asyncio.run(main.search_soulseek("Battleground of Broken Hearts"))
    assert len(results) == 1
    assert results[0]["filename"] == filename
    assert sleep.call_count == 17
    assert slsk.call_args_list[0].kwargs["json"]["searchTimeout"] == 15000


def test_soulseek_download_preserves_audio_and_scan_warning(client, monkeypatch):
    job = seed_job()
    folder = main.DOWNLOADS / "home-music-request" / job["id"]
    folder.mkdir(parents=True)
    audio = folder / "Track.flac"
    audio.write_bytes(b"audio-data")
    selection = dict(SELECTION, size=audio.stat().st_size)
    job["selection"] = json.dumps(selection)
    states = []
    original_update = main.update

    def record_update(job_id, **kwargs):
        if "status" in kwargs:
            states.append(kwargs["status"])
        original_update(job_id, **kwargs)

    monkeypatch.setattr(main, "update", record_update)
    monkeypatch.setattr(main, "slsk", AsyncMock(side_effect=[
        {"batch": {"transfers": [{"id": "transfer"}]}, "failures": []},
        {"transfers": [{"state": "Completed, Succeeded", "percentComplete": 100}]}]))
    def audio_metadata(path):
        with main.connect() as db:
            assert db.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()["status"] == "Processing"
        return type("Audio", (), {"info": type("Info", (), {"length": 30})()})()

    monkeypatch.setattr(main, "AudioFile", audio_metadata)
    monkeypatch.setattr(main, "command", AsyncMock(side_effect=AssertionError("FLAC must not be converted")))
    monkeypatch.setattr(main, "navidrome", AsyncMock(side_effect=RuntimeError("scan denied")))
    asyncio.run(main.download(job))
    result = client.get("/api/downloads").json()[0]
    assert states == ["Downloading", "Complete"]
    assert result["status"] == "Complete" and result["warning"]
    assert (main.LIBRARY / result["path"]).read_bytes() == b"audio-data"
    assert not audio.exists()
    enqueue = main.slsk.call_args_list[0].kwargs["json"]
    assert enqueue["options"]["destination"] == f"home-music-request/{job['id']}"


def test_completed_failed_transfer_is_not_imported(client, monkeypatch):
    monkeypatch.setattr(main, "slsk", AsyncMock(side_effect=[
        {"batch": {"transfers": [{}]}, "failures": []},
        {"transfers": [{"state": "Completed, Errored"}]}]))
    imported = AsyncMock()
    monkeypatch.setattr(main, "navidrome", imported)
    with pytest.raises(RuntimeError, match="transfer failed"):
        asyncio.run(main.download(seed_job()))
    imported.assert_not_called()


def test_enqueue_failure(client, monkeypatch):
    monkeypatch.setattr(main, "slsk", AsyncMock(return_value={"failures": [{"filename": "Track"}]}))
    with pytest.raises(RuntimeError, match="could not enqueue"):
        asyncio.run(main.download(seed_job()))


def test_audio_validation_and_atomic_import(client, tmp_path):
    source = tmp_path / "audio.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 800)
    original = source.read_bytes()
    path = main.import_audio(source, str(uuid.uuid4()))
    assert (main.LIBRARY / path).read_bytes() == original
    assert not source.exists()
    assert not list(main.LIBRARY.rglob("*.part"))
    invalid = tmp_path / "invalid.mp3"
    invalid.write_bytes(b"this is not audio")
    with pytest.raises(Exception):
        main.import_audio(invalid, str(uuid.uuid4()))
    assert invalid.exists()


def test_youtube_stream_copy(client, monkeypatch):
    job = seed_job({"source": "youtube", "title": "Track", "video_id": "abc123"})
    calls = []

    async def command(*args, **kwargs):
        calls.append(args)
        if args[0] == "yt-dlp":
            (main.DATA / "staging" / job["id"] / "Track.webm").write_bytes(b"webm")
        else:
            assert args[args.index("-c:a") + 1] == "copy"
            from pathlib import Path
            Path(args[-1]).write_bytes(b"opus")

    monkeypatch.setattr(main, "command", command)
    tag_audio = Mock()
    monkeypatch.setattr(main, "tag_youtube_audio", tag_audio)
    monkeypatch.setattr(main, "import_audio", lambda source, job_id: "Track.opus")
    monkeypatch.setattr(main, "navidrome", AsyncMock(return_value={}))
    asyncio.run(main.download(job))
    assert [args[0] for args in calls] == ["yt-dlp", "ffmpeg"]
    assert calls[0][calls[0].index("--progress-template") + 1] == "download:hmr:%(progress)j"
    tag_audio.assert_called_once()
    assert client.get("/api/downloads").json()[0]["status"] == "Complete"


@pytest.mark.parametrize("video_title,existing,expected", [
    ("Artist - Song | Alternate language", {}, {"artist": ["Artist"], "title": ["Song"]}),
    ("Live recording", {}, {"title": ["Live recording"]}),
    ("Upload Artist - Upload Title", {"artist": ["Actual Artist"], "title": ["Actual Title"]},
     {"artist": ["Actual Artist"], "title": ["Actual Title"]}),
])
def test_youtube_tags(monkeypatch, video_title, existing, expected):
    class Audio(dict):
        tags = None
        saved = False

        def add_tags(self):
            self.tags = self

        def save(self):
            self.saved = True

    audio = Audio(existing)
    monkeypatch.setattr(main, "AudioFile", lambda source, easy: audio)
    main.tag_youtube_audio("track.m4a", video_title)
    assert dict(audio) == expected
    assert audio.saved


def test_navidrome_rejects_http_200_api_failure(client, monkeypatch):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, params):
            assert "p" not in params
            assert len(params["t"]) == 32
            return main.httpx.Response(200, json={"subsonic-response": {"status": "failed", "error": {"code": 50}}},
                                       request=main.httpx.Request("GET", url))

    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome:4533")
    monkeypatch.setenv("NAVIDROME_USER", "admin")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "secret")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    with pytest.raises(RuntimeError, match="rejected"):
        asyncio.run(main.navidrome("startScan"))


def test_scan_retry(client, monkeypatch):
    monkeypatch.setattr(main, "navidrome", AsyncMock(return_value={}))
    assert client.post("/api/scan", headers=HEADERS).status_code == 200
    monkeypatch.setattr(main, "navidrome", AsyncMock(side_effect=RuntimeError("private detail")))
    response = client.post("/api/scan", headers=HEADERS)
    assert response.status_code == 502
    assert "private detail" not in response.text


def test_progress_and_stall_detection(client, monkeypatch):
    job = seed_job(status="Downloading")
    monkeypatch.setattr(main.time, "time", lambda: 1000)
    main.record_progress(job["id"], 25, total=100, speed=10, eta=7.5, transfer_state="InProgress")
    result = client.get("/api/downloads").json()[0]
    assert result["progress"] == 25
    assert result["downloaded_bytes"] == 25 and result["total_bytes"] == 100
    assert result["speed"] == 10 and result["eta"] == 7.5
    assert result["last_activity"] == 1000 and not result["stalled"]
    monkeypatch.setattr(main.time, "time", lambda: 1065)
    main.record_progress(job["id"], 25, total=100, speed=10, eta=7.5, transfer_state="InProgress")
    result = client.get("/api/downloads").json()[0]
    assert result["stalled"] and result["idle_seconds"] == 65
    assert result["speed"] == 0 and result["eta"] is None
    assert result["last_activity"] == 1000 and result["last_update"] == 1065
    main.record_progress(job["id"], 50, total=100, speed=20, eta=2.5, transfer_state="InProgress")
    result = client.get("/api/downloads").json()[0]
    assert result["progress"] == 50 and not result["stalled"]


def test_peer_queue_is_waiting_not_stalled(client, monkeypatch):
    job = seed_job(status="Downloading")
    monkeypatch.setattr(main.time, "time", lambda: 1000)
    main.record_progress(job["id"], 0, total=100, transfer_state="Queued, Remotely", queue_position=3)
    monkeypatch.setattr(main.time, "time", lambda: 1100)
    result = client.get("/api/downloads").json()[0]
    assert result["waiting_for_peer"] and not result["stalled"]
    assert result["queue_position"] == 3
    main.update(job["id"], status="Processing")
    assert not client.get("/api/downloads").json()[0]["waiting_for_peer"]


def test_unknown_and_estimated_sizes(client):
    job = seed_job(status="Downloading")
    main.record_progress(job["id"], 20, transfer_state="Downloading audio")
    result = client.get("/api/downloads").json()[0]
    assert result["total_bytes"] is None and result["downloaded_bytes"] == 20
    main.record_progress(job["id"], 50, total=40, transfer_state="Downloading audio")
    assert client.get("/api/downloads").json()[0]["progress"] == 100
    main.record_progress(job["id"], 60, total=100, transfer_state="Downloading audio")
    assert client.get("/api/downloads").json()[0]["progress"] == 60


def test_youtube_progress_stream_updates_before_exit(client):
    job = seed_job(status="Downloading")
    script = (
        "import json,time; "
        "print('irrelevant output',flush=True); print('hmr:not-json',flush=True); "
        "print('hmr:'+json.dumps({'status':'downloading','downloaded_bytes':25,"
        "'total_bytes':100,'speed':10,'eta':7.5}),flush=True); "
        "time.sleep(0.5); "
        "print('hmr:'+json.dumps({'status':'finished','downloaded_bytes':100,'total_bytes':100}),flush=True)"
    )

    async def run():
        task = asyncio.create_task(main.command(sys.executable, "-c", script, progress_job=job["id"]))
        for _ in range(100):
            await asyncio.sleep(0.01)
            with main.connect() as db:
                progress = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if progress["downloaded_bytes"] == 25:
                assert not task.done()
                assert progress["speed"] == 10
                break
        else:
            pytest.fail("No progress was recorded while the subprocess was running")
        await task

    asyncio.run(run())
    result = client.get("/api/downloads").json()[0]
    assert result["progress"] == 100
    assert result["transfer_state"] == "Download finished"


def test_progress_process_timeout(client):
    job = seed_job(status="Downloading")
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(main.command(sys.executable, "-c", "import time; time.sleep(10)",
                                 timeout=0.1, progress_job=job["id"]))


def test_real_ytdlp_progress_format(client, tmp_path):
    # Generated audio served only on loopback verifies yt-dlp's actual progress
    # format without downloading third-party media or touching the library.
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 100000)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    job = seed_job(status="Downloading")
    try:
        asyncio.run(main.command(sys.executable, "-m", "yt_dlp", "--ignore-config", "--quiet", "--progress",
                                 "--newline", "--progress-delta", "1", "--progress-template", "download:hmr:%(progress)j",
                                 "-o", str(tmp_path / "download.wav"), "--",
                                 f"http://127.0.0.1:{server.server_port}/audio.wav", progress_job=job["id"], timeout=30))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    result = client.get("/api/downloads").json()[0]
    assert result["downloaded_bytes"] == audio.stat().st_size
    assert result["progress"] == 100 and result["transfer_state"] == "Download finished"


def test_metrics_migrate_without_losing_existing_jobs(client):
    job = seed_job()
    with main.connect() as db:
        for name in ("downloaded_bytes", "total_bytes", "speed", "eta", "transfer_state", "queue_position", "last_activity", "last_update"):
            db.execute(f"ALTER TABLE jobs DROP COLUMN {name}")
    with TestClient(main.app) as restarted:
        restarted.auth = client.auth
        result = restarted.get("/api/downloads").json()[0]
        assert result["id"] == job["id"] and result["status"] == "Queued"
        assert result["downloaded_bytes"] == 0 and result["total_bytes"] is None


def test_cancel_queued_and_idempotent(client, monkeypatch):
    job = seed_job()
    upstream = AsyncMock()
    monkeypatch.setattr(main, "slsk", upstream)
    response = client.post(f"/api/downloads/{job['id']}/cancel", headers=HEADERS)
    assert response.status_code == 202 and response.json()["status"] == "Cancelled"
    assert client.post(f"/api/downloads/{job['id']}/cancel", headers=HEADERS).json() == response.json()
    assert client.get("/api/downloads").json()[0]["status"] == "Cancelled"
    with pytest.raises(main.DownloadCancelled):
        asyncio.run(main.download(job))
    assert asyncio.run(main.cancel_download(job))
    upstream.assert_not_called()


@pytest.mark.parametrize("status", ["Processing", "Complete", "Failed"])
def test_cancel_rejects_finished_or_processing_jobs(client, status):
    job = seed_job(status=status)
    response = client.post(f"/api/downloads/{job['id']}/cancel", headers=HEADERS)
    assert response.status_code == 409
    assert client.get("/api/downloads").json()[0]["status"] == status


def test_cancel_missing_and_cross_site(client):
    assert client.post("/api/downloads/missing/cancel", headers=HEADERS).status_code == 404
    job = seed_job()
    assert client.post(f"/api/downloads/{job['id']}/cancel").status_code == 403
    assert client.post(f"/api/downloads/{job['id']}/cancel", headers={**HEADERS, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/downloads").json()[0]["status"] == "Queued"


def test_soulseek_cancel_targets_own_transfer_and_confirms(client, monkeypatch):
    selection = dict(SELECTION, username="peer name/#")
    job = seed_job(selection, status="Downloading")
    assert client.post(f"/api/downloads/{job['id']}/cancel", headers=HEADERS).json()["status"] == "Cancelling"
    slsk = AsyncMock(side_effect=[
        {"transfers": [{"id": "transfer-id", "state": "InProgress"}]}, None,
        {"transfers": [{"id": "transfer-id", "state": "InProgress"}]},
        {"transfers": [{"id": "transfer-id", "state": "Completed, Cancelled"}]},
    ])
    monkeypatch.setattr(main, "slsk", slsk)
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    assert asyncio.run(main.cancel_download(job))
    assert slsk.call_args_list[1].args == ("DELETE", "/transfers/downloads/peer%20name%2F%23/transfer-id")
    assert slsk.call_args_list[0].args[1] == f"/transfers/downloads/batches/{job['id']}"
    result = client.get("/api/downloads").json()[0]
    assert result["status"] == "Cancelled" and result["path"] is None and result["speed"] == 0


@pytest.mark.parametrize("response", [None, {"transfers": []}])
def test_cancellation_failure_does_not_claim_success(client, monkeypatch, response):
    job = seed_job(status="Cancelling")
    mock = AsyncMock(side_effect=RuntimeError("upstream unavailable")) if response is None else AsyncMock(return_value=response)
    monkeypatch.setattr(main, "slsk", mock)
    assert not asyncio.run(main.cancel_download(job))
    result = client.get("/api/downloads").json()[0]
    assert result["status"] == "Cancelling" and "Could not confirm" in result["error"]
    assert result["path"] is None


def test_cancel_during_soulseek_enqueue_blocks_import(client, monkeypatch):
    job = seed_job()

    async def enqueue(*args, **kwargs):
        main.update(job["id"], status="Cancelling")
        return {"batch": {"transfers": [{"id": "transfer"}]}, "failures": []}

    monkeypatch.setattr(main, "slsk", enqueue)
    imported = Mock()
    monkeypatch.setattr(main, "import_audio", imported)
    with pytest.raises(main.DownloadCancelled):
        asyncio.run(main.download(job))
    imported.assert_not_called()
    assert client.get("/api/downloads").json()[0]["status"] == "Cancelling"


def test_cancel_at_processing_boundary_blocks_import(client, monkeypatch):
    job = seed_job({"source": "youtube", "title": "Track", "video_id": "abc123"})

    async def command(*args, **kwargs):
        (main.DATA / "staging" / job["id"] / "Track.m4a").write_bytes(b"audio")
        main.update(job["id"], status="Cancelling")

    monkeypatch.setattr(main, "command", command)
    imported = Mock()
    monkeypatch.setattr(main, "import_audio", imported)
    with pytest.raises(main.DownloadCancelled):
        asyncio.run(main.download(job))
    imported.assert_not_called()
    assert client.get("/api/downloads").json()[0]["status"] == "Cancelling"


def test_youtube_cancel_kills_process_and_removes_only_own_staging(client, monkeypatch):
    job = seed_job({"source": "youtube", "title": "Track", "video_id": "abc123"}, status="Downloading")
    folder = main.DATA / "staging" / job["id"]
    folder.mkdir(parents=True)
    (folder / "Track.m4a.part").write_bytes(b"partial")
    other = main.DATA / "staging" / "other-job"
    other.mkdir()
    (other / "Other.m4a").write_bytes(b"keep")
    spawned = []
    spawn = main.asyncio.create_subprocess_exec

    async def capture_process(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", capture_process)
    script = "import time; print('hmr:{\"status\":\"downloading\",\"downloaded_bytes\":1,\"total_bytes\":100}',flush=True); time.sleep(10)"

    async def run():
        task = asyncio.create_task(main.command(sys.executable, "-c", script, progress_job=job["id"]))
        for _ in range(100):
            await asyncio.sleep(0.01)
            with main.connect() as db:
                downloaded = db.execute("SELECT downloaded_bytes FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
            if downloaded:
                break
        else:
            pytest.fail("Subprocess did not start downloading")
        await main.cancel(job["id"])
        with pytest.raises(main.DownloadCancelled):
            await asyncio.wait_for(task, 3)
        assert spawned[0].returncode is not None
        assert await main.cancel_download(job)

    asyncio.run(run())
    assert not folder.exists()
    assert (other / "Other.m4a").read_bytes() == b"keep"
    assert client.get("/api/downloads").json()[0]["status"] == "Cancelled"


def test_slsk_accepts_empty_cancel_response(client, monkeypatch):
    async def request(*args, **kwargs):
        return main.httpx.Response(204, request=main.httpx.Request("DELETE", "http://slskd/api/v0/transfers"))

    monkeypatch.setenv("SLSKD_URL", "http://slskd:5030")
    monkeypatch.setenv("SLSKD_API_KEY", "private-key")
    monkeypatch.setattr(main.httpx.AsyncClient, "request", request)
    assert asyncio.run(main.slsk("DELETE", "/transfers/downloads/peer/transfer")) is None


def test_pending_cancellation_survives_restart(client):
    job = seed_job(status="Cancelling")
    with TestClient(main.app) as restarted:
        result = restarted.get("/api/downloads").json()[0]
        assert result["id"] == job["id"] and result["status"] == "Cancelling"


def test_worker_handles_cancellation_and_continues_queue(client, monkeypatch):
    cancelled = seed_job(status="Cancelling")
    following = seed_job({"source": "youtube", "title": "Next", "video_id": "next"})
    seen = []

    async def cancel_download(job):
        seen.append(("cancel", job["id"]))
        main.update(job["id"], status="Cancelled")
        return True

    async def download(job):
        seen.append(("download", job["id"]))
        main.update(job["id"], status="Complete")

    monkeypatch.setattr(main, "cancel_download", cancel_download)
    monkeypatch.setattr(main, "download", download)

    async def run():
        worker = asyncio.create_task(WORKER())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(seen) == 2:
                break
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    asyncio.run(run())
    assert seen == [("cancel", cancelled["id"]), ("download", following["id"])]
