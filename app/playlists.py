import csv
import io
import json
import math
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

MAX_TRACKS = 500
MAX_IMPORT_BYTES = 2 * 1024 * 1024


def spotify_playlist_id(url):
    parsed = urlsplit(url.strip())
    match = re.fullmatch(r"/(?:intl-[a-zA-Z-]+/)?playlist/([a-zA-Z0-9]{22})/?", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "open.spotify.com" or not match:
        raise ValueError("Use an https://open.spotify.com/playlist/... URL, not a shortened or private server URL")
    return match[1]


class SpotifyDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capturing = False
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.capturing = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.capturing = False

    def handle_data(self, data):
        if self.capturing:
            self.chunks.append(data)


def parse_spotify_preview(html, playlist_id):
    parser = SpotifyDataParser()
    parser.feed(html)
    try:
        payload = json.loads("".join(parser.chunks))
        entity = payload["props"]["pageProps"]["state"]["data"]["entity"]
        if entity["type"] != "playlist" or entity["id"] != playlist_id:
            raise ValueError("Unexpected Spotify entity")
        tracks = []
        for entry in entity["trackList"]:
            if entry.get("entityType", "track") != "track":
                continue
            title, artist = entry["title"].strip(), entry["subtitle"].strip()
            if not title or not artist or len(title) > 200 or len(artist) > 200:
                raise ValueError("Invalid track metadata")
            milliseconds = entry.get("duration")
            duration = float(milliseconds) / 1000 if milliseconds is not None else None
            if duration is not None and (not math.isfinite(duration) or not 0 <= duration <= 86400):
                raise ValueError("Invalid track duration")
            uri = entry.get("uri", "")
            tracks.append(dict(title=title, artist=artist, album="", duration=duration,
                               spotify_uri=uri if re.fullmatch(r"spotify:track:[a-zA-Z0-9]{22}", uri) else None))
        if not tracks or len(tracks) > MAX_TRACKS:
            raise ValueError("No usable preview tracks")
        name = entity["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("No playlist name")
        return dict(name=name[:300], source="spotify", preview=True,
                    warning="Spotify exposes a public preview, which may omit tracks. Upload an exported CSV for the full list. No audio is downloaded from Spotify.",
                    tracks=tracks)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError("Spotify did not expose a usable playlist preview. Check that the playlist is public, or upload an exported CSV.") from exc


def parse_csv_playlist(text):
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise ValueError("CSV is empty")
    try:
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
        headers = reader.fieldnames or []
        normalized = {re.sub(r"[^a-z0-9]", "", header.lower()): header for header in headers}
        if len(normalized) != len(headers):
            raise ValueError("CSV has duplicate or ambiguous column names")

        def column(*aliases):
            return next((normalized[name] for name in aliases if name in normalized), None)

        title_column = column("trackname", "title", "song", "songname", "name")
        artist_column = column("artistnames", "artistname", "artists", "artist")
        album_column = column("albumname", "album")
        milliseconds_column = column("trackdurationms", "durationms")
        seconds_column = column("durationseconds", "duration", "length")
        uri_column = column("trackuri", "spotifyuri", "uri")
        if not title_column or not artist_column:
            raise ValueError("CSV needs Title and Artist columns (Track Name and Artist Name(s) are also supported)")
        tracks = []
        for row in reader:
            if None in row:
                raise ValueError(f"CSV row near line {reader.line_num} has more values than columns")
            if not any(value and value.strip() for value in row.values()):
                continue
            title = (row.get(title_column) or "").strip()
            artist = (row.get(artist_column) or "").strip()
            album = (row.get(album_column) or "").strip()
            if not title or not artist or len(title) > 200 or len(artist) > 200 or len(album) > 300:
                raise ValueError(f"CSV row near line {reader.line_num} needs a title and artist, each at most 200 characters (album at most 300)")
            duration = None
            value = (row.get(milliseconds_column) or row.get(seconds_column) or "").strip()
            if value:
                try:
                    if milliseconds_column and row.get(milliseconds_column):
                        duration = float(value) / 1000
                    elif ":" in value:
                        parts = [float(part) for part in value.split(":")]
                        if len(parts) not in {2, 3} or any(not 0 <= part < 60 for part in parts[1:]):
                            raise ValueError()
                        duration = sum(part * 60 ** index for index, part in enumerate(reversed(parts)))
                    else:
                        duration = float(value)
                    if not math.isfinite(duration) or not 0 <= duration <= 86400:
                        raise ValueError()
                except ValueError:
                    raise ValueError(f"CSV row near line {reader.line_num} has an invalid duration; use seconds, mm:ss, or Track Duration (ms)")
            uri = (row.get(uri_column) or "").strip()
            tracks.append(dict(title=title, artist=artist, album=album, duration=duration,
                               spotify_uri=uri if re.fullmatch(r"spotify:track:[a-zA-Z0-9]{22}", uri) else None))
            if len(tracks) > MAX_TRACKS:
                raise ValueError(f"Import at most {MAX_TRACKS} tracks at a time")
        if not tracks:
            raise ValueError("CSV contains no tracks")
        return dict(name="CSV Import", source="csv", preview=False, warning=None, tracks=tracks)
    except csv.Error as exc:
        raise ValueError("CSV could not be parsed; check its delimiter and quoted fields") from exc
