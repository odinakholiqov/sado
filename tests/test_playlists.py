import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from app import main, playlists

HEADERS = {"X-Requested-With": "HomeMusicRequest"}
PLAYLIST_ID = "4aTVBT9etwl886iEi8hFIK"
PLAYLIST_URL = f"https://open.spotify.com/playlist/{PLAYLIST_ID}"
TRACK = {"title": "Thank You", "artist": "Dido", "album": "No Angel", "duration": 218, "spotify_uri": None}


def spotify_html():
    entity = {"type": "playlist", "id": PLAYLIST_ID, "name": "TOO GOOD", "trackList": [
        {"title": "Thank You", "subtitle": "Dido", "duration": 218360,
         "uri": "spotify:track:3yUcJwYu7fXAfqMj9krY6l", "entityType": "track"},
        {"title": "Episode", "subtitle": "Podcast", "entityType": "episode"}]}
    payload = {"props": {"pageProps": {"state": {"data": {"entity": entity}, "settings": {"accessToken": "do-not-expose"}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></html>'


def mock_spotify(monkeypatch, content, status=200):
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @asynccontextmanager
        async def stream(self, method, url):
            calls.append((method, url))
            yield httpx.Response(status, content=content, request=httpx.Request(method, url))

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        return Client()

    monkeypatch.setattr(main.httpx, "AsyncClient", factory)
    return calls


def seed_selection(title="Track", created=None):
    selection_id = str(uuid.uuid4())
    payload = {"source": "youtube", "title": title, "video_id": "safe-video-id"}
    with main.connect() as db:
        db.execute("INSERT INTO selections VALUES (?,?,?)", (selection_id, json.dumps(payload), created if created is not None else time.time()))
    return selection_id


@pytest.mark.parametrize("url", [PLAYLIST_URL, PLAYLIST_URL + "?si=anything#fragment", PLAYLIST_URL.replace("/playlist/", "/intl-en/playlist/")])
def test_spotify_url_normalization(url):
    assert playlists.spotify_playlist_id(url) == PLAYLIST_ID


@pytest.mark.parametrize("url", [
    "http://open.spotify.com/playlist/" + PLAYLIST_ID,
    "https://evil.example/playlist/" + PLAYLIST_ID,
    "https://open.spotify.com.evil.example/playlist/" + PLAYLIST_ID,
    "https://open.spotify.com@127.0.0.1/playlist/" + PLAYLIST_ID,
    "https://open.spotify.com:443/playlist/" + PLAYLIST_ID,
    "https://open.spotify.com/album/" + PLAYLIST_ID,
    "https://open.spotify.com/playlist/../../private",
    "https://spotify.link/short",
])
def test_spotify_rejects_arbitrary_hosts_and_paths(url):
    with pytest.raises(ValueError):
        playlists.spotify_playlist_id(url)


def test_spotify_parser_metadata_only():
    result = playlists.parse_spotify_preview(spotify_html(), PLAYLIST_ID)
    assert result["name"] == "TOO GOOD" and result["preview"]
    assert len(result["tracks"]) == 1
    assert result["tracks"][0]["duration"] == 218.36
    assert "may omit tracks" in result["warning"]
    assert "do-not-expose" not in json.dumps(result)
    assert "accessToken" not in json.dumps(result)


def test_spotify_import_does_not_search_or_download(client, monkeypatch):
    calls = mock_spotify(monkeypatch, spotify_html())
    soulseek = AsyncMock(side_effect=AssertionError("Import must not search"))
    monkeypatch.setattr(main, "search_soulseek", soulseek)
    monkeypatch.setattr(main, "navidrome", soulseek)
    response = client.post("/api/playlists/spotify", headers=HEADERS, json={"url": PLAYLIST_URL + "?si=private-share-token"})
    assert response.status_code == 200
    assert calls == [("GET", f"https://open.spotify.com/embed/playlist/{PLAYLIST_ID}")]
    assert response.json()["tracks"][0]["title"] == "Thank You"
    assert client.get("/api/downloads").json() == []
    soulseek.assert_not_called()


def test_spotify_bad_url_never_fetched(client, monkeypatch):
    calls = mock_spotify(monkeypatch, spotify_html())
    assert client.post("/api/playlists/spotify", headers=HEADERS, json={"url": "https://127.0.0.1/private"}).status_code == 400
    assert calls == []


@pytest.mark.parametrize("content,status", [("<html>Private</html>", 200), ("", 302), ("", 404), ("x" * (playlists.MAX_IMPORT_BYTES + 1), 200)])
def test_spotify_unavailable_redirect_and_size_errors(client, monkeypatch, content, status):
    calls = mock_spotify(monkeypatch, content, status)
    response = client.post("/api/playlists/spotify", headers=HEADERS, json={"url": PLAYLIST_URL})
    assert response.status_code == 502
    assert len(calls) == 1
    assert client.get("/api/downloads").json() == []


def test_wrong_spotify_entity_rejected():
    with pytest.raises(ValueError, match="usable playlist preview"):
        playlists.parse_spotify_preview(spotify_html(), "a" * 22)


def test_csv_exportify_unicode_and_quoted_fields():
    text = '\ufeffTrack URI,Track Name,Artist Name(s),Album Name,Track Duration (ms)\nspotify:track:3yUcJwYu7fXAfqMj9krY6l,"Thank You, Again","Dido, Guest",No Angel,218360\n,\u0414\u0430\u0440 \u0431\u0430\u0440\u0438 \u0442\u0443,\u041d\u0430\u0440\u0433\u0438\u0441,,215000\n'
    result = playlists.parse_csv_playlist(text)
    assert not result["preview"] and result["warning"] is None
    assert result["tracks"][0]["title"] == "Thank You, Again"
    assert result["tracks"][0]["artist"] == "Dido, Guest"
    assert result["tracks"][0]["duration"] == 218.36
    assert result["tracks"][1]["artist"] == "\u041d\u0430\u0440\u0433\u0438\u0441"


@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
def test_csv_simple_headers_and_duration(delimiter):
    result = playlists.parse_csv_playlist(delimiter.join(["Title", "Artist", "Album", "Duration"]) + "\n" + delimiter.join(["Song", "Artist", "Album", "3:36"]))
    assert result["tracks"][0] == {"title": "Song", "artist": "Artist", "album": "Album", "duration": 216, "spotify_uri": None}


@pytest.mark.parametrize("text", [
    "", "Title\nSong", "Title,Artist\n,Artist", "Title,Artist\nSong,", "Title,title,Artist\nSong,Song,Artist",
    'Title,Artist\n"unclosed,Artist', "Title,Artist\nSong,Artist,extra",
    "Title,Artist,Duration\nSong,Artist,nan", "Title,Artist,Duration\nSong,Artist,3:78",
    "Title,Artist,Duration\nSong,Artist,-1", "Title,Artist\n" + "x" * 201 + ",Artist",
])
def test_csv_invalid_inputs_rejected(text):
    with pytest.raises(ValueError):
        playlists.parse_csv_playlist(text)


def test_csv_count_limit():
    assert len(playlists.parse_csv_playlist("Title,Artist\n" + "Song,Artist\n" * 500)["tracks"]) == 500
    with pytest.raises(ValueError, match="at most 500"):
        playlists.parse_csv_playlist("Title,Artist\n" + "Song,Artist\n" * 501)


def test_csv_endpoint_encoding_and_body_limit(client):
    response = client.post("/api/playlists/csv", headers={**HEADERS, "Content-Type": "text/csv"}, content=b"Title,Artist\nThank You,Dido\n")
    assert response.status_code == 200 and len(response.json()["tracks"]) == 1
    assert client.get("/api/downloads").json() == []
    assert client.post("/api/playlists/csv", headers=HEADERS, content=b"Title,Artist\n\xff,Dido").status_code == 400
    assert client.post("/api/playlists/csv", headers=HEADERS, content=b"x" * (playlists.MAX_IMPORT_BYTES + 1)).status_code == 413
    assert client.post("/api/playlists/csv", headers=HEADERS, content="Wrong,Headers\nSong,Artist").status_code == 400


def test_library_checks_bounded_concurrency_and_preserves_rows(client, monkeypatch):
    running = peak = 0

    async def library(method, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        assert method == "search3" and kwargs["songCount"] == 10
        return {"searchResult3": {"song": [{"title": kwargs["query"], "artist": "Possible artist"}]}}

    monkeypatch.setattr(main, "navidrome", library)
    rows = [{**TRACK, "title": f"Song {i}"} for i in range(12)]
    response = client.post("/api/playlists/check", headers=HEADERS, json={"tracks": rows})
    assert response.status_code == 200 and peak == 4
    checked = response.json()["tracks"]
    assert [r["title"] for r in checked] == [r["title"] for r in rows]
    assert all(r["local"] and r["library_error"] is None for r in checked)
    assert client.get("/api/downloads").json() == []


def test_library_partial_failure_is_visible_and_does_not_fan_out(client, monkeypatch):
    mock = AsyncMock(side_effect=[{"searchResult3": {"song": [{"title": "Thank You"}]}}, RuntimeError("private upstream error")])
    monkeypatch.setattr(main, "navidrome", mock)
    response = client.post("/api/playlists/check", headers=HEADERS, json={"tracks": [TRACK] * 10})
    rows = response.json()["tracks"]
    assert rows[0]["local"] and rows[0]["library_error"] is None
    assert all(row["library_error"] for row in rows[1:])
    assert "private upstream" not in response.text
    assert mock.call_count == 2


@pytest.mark.parametrize("tracks", [[], [TRACK] * 501, [{**TRACK, "artist": "   "}], [{**TRACK, "duration": -1}]])
def test_library_request_validation(client, tracks):
    assert client.post("/api/playlists/check", headers=HEADERS, json={"tracks": tracks}).status_code == 422


def test_batch_queues_only_selected_server_results_and_is_idempotent(client):
    first, second = seed_selection("First"), seed_selection("Second")
    seed_selection("Not chosen")
    body = {"selection_ids": [second, first, second], "permitted": True}
    response = client.post("/api/downloads/batch", headers=HEADERS, json=body)
    assert response.status_code == 202
    assert [row["selection_id"] for row in response.json()["jobs"]] == [second, first]
    assert client.post("/api/downloads/batch", headers=HEADERS, json=body).json() == response.json()
    assert {row["title"] for row in client.get("/api/downloads").json()} == {"First", "Second"}


def test_batch_permission_expiry_and_atomic_validation(client):
    valid = seed_selection()
    expired = seed_selection(created=0)
    assert client.post("/api/downloads/batch", headers=HEADERS, json={"selection_ids": [valid]}).status_code == 400
    for missing in [expired, str(uuid.uuid4())]:
        response = client.post("/api/downloads/batch", headers=HEADERS, json={"selection_ids": [valid, missing], "permitted": True})
        assert response.status_code == 404
        assert client.get("/api/downloads").json() == []


@pytest.mark.parametrize("ids", [[], ["x"] * 101, ["x" * 101]])
def test_batch_limits(client, ids):
    assert client.post("/api/downloads/batch", headers=HEADERS, json={"selection_ids": ids, "permitted": True}).status_code == 422


def test_playlist_routes_keep_cross_site_protection(client):
    assert client.post("/api/playlists/spotify", json={"url": PLAYLIST_URL}).status_code == 403
    assert client.post("/api/playlists/csv", content="Title,Artist\nSong,Artist").status_code == 403
    assert client.post("/api/playlists/check", json={"tracks": [TRACK]}).status_code == 403
    assert client.post("/api/downloads/batch", json={"selection_ids": ["x"]}).status_code == 403


def test_import_check_search_choose_batch_workflow(client, monkeypatch):
    imported = client.post("/api/playlists/csv", headers=HEADERS, content="Title,Artist\nThank You,Dido\nGhetto,Akon\n").json()
    assert len(imported["tracks"]) == 2
    assert client.get("/api/downloads").json() == []
    monkeypatch.setattr(main, "navidrome", AsyncMock(return_value={"searchResult3": {}}))
    checked = client.post("/api/playlists/check", headers=HEADERS, json={"tracks": imported["tracks"]}).json()
    assert all(not row["local"] and not row["library_error"] for row in checked["tracks"])
    monkeypatch.setattr(main, "search_soulseek", AsyncMock(return_value=[{
        "source": "soulseek", "title": "Thank You live.mp3", "username": "peer", "filename": "Music\\Thank You live.mp3",
        "size": 100, "format": "mp3", "search_id": "search"}]))
    monkeypatch.setattr(main, "search_youtube", AsyncMock(return_value=[{"source": "youtube", "title": "Thank You", "video_id": "video"}]))
    recordings = client.get("/api/search", params={"q": "Dido Thank You"}).json()["results"]
    assert len(recordings) == 2 and client.get("/api/downloads").json() == []
    # The caller explicitly chooses one exact recording, not both search results
    # and not the unsearched second playlist track.
    response = client.post("/api/downloads/batch", headers=HEADERS, json={"selection_ids": [recordings[1]["id"]], "permitted": True})
    assert response.status_code == 202
    jobs = client.get("/api/downloads").json()
    assert len(jobs) == 1 and jobs[0]["source"] == "youtube"
    assert client.post(f"/api/downloads/{jobs[0]['id']}/cancel", headers=HEADERS).json()["status"] == "Cancelled"
