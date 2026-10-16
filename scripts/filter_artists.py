import base64
import json
import os
import re
import time
from pathlib import Path

import requests
from langdetect import detect, LangDetectException

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

STATE_FILE = OUTPUT_DIR / "state.json"
ACTIVE_FILE = OUTPUT_DIR / "artistes_actifs_2026.json"
FRENCH_FILE = OUTPUT_DIR / "artistes_francophones_2026.json"
REPORT_FILE = OUTPUT_DIR / "rapport_exclusions.json"

SOURCE_FILES = [
    DATA_DIR / "artistes.json",
    DATA_DIR / "artistes_1.json",
    DATA_DIR / "artistes_2.json",
]

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
LRCLIB_API = "https://lrclib.net/api/search"

RELEASE_YEAR = 2026
MIN_LISTENERS = int(os.getenv("MIN_MONTHLY_LISTENERS", "0"))
MAX_ARTISTS = 25 #int(os.getenv("MAX_ARTISTS", "500"))

FRENCH_MARKERS = re.compile(
    r"\b(je|tu|il|elle|nous|vous|ils|elles|mais|donc|alors|avec|sans|pour|dans|sur|"
    r"parce|quand|jamais|toujours|encore|maintenant|c'est|j'ai|t'es|on est|ça|ça va)\b",
    re.IGNORECASE,
)


def load_artists():
    artists = {}

    for path in SOURCE_FILES:
        if not path.exists():
            print(f"Fichier absent, ignoré : {path}")
            continue

        with path.open(encoding="utf-8") as f:
            content = json.load(f)

        for artist_id, artist in content.get("artists", {}).items():
            artist["id"] = artist.get("id", artist_id)
            artists[artist_id] = artist

    print(f"Artistes uniques chargés : {len(artists)}")
    return artists


def load_json(path, default):
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class SpotifyClient:
    def __init__(self):
        client_id = os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

        if not client_id or not client_secret:
            raise SystemExit(
                "Ajoute les secrets GitHub SPOTIFY_CLIENT_ID et SPOTIFY_CLIENT_SECRET."
            )

        self.token = None
        self.token_expires_at = 0

    def get_token(self):
        now = time.time()

        if self.token and now < self.token_expires_at - 60:
            return self.token

        credentials = base64.b64encode(
            f"{os.getenv('SPOTIFY_CLIENT_ID')}:{os.getenv('SPOTIFY_CLIENT_SECRET')}".encode()
        ).decode()

        response = requests.post(
            SPOTIFY_TOKEN_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()

        self.token = payload["access_token"]
        self.token_expires_at = now + payload["expires_in"]
        return self.token

    def get(self, url, params=None):
        for attempt in range(5):
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {self.get_token()}"},
                params=params,
                timeout=30,
            )

            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", "5"))
                print(f"Rate limit Spotify : attente de {wait} secondes")
                time.sleep(wait)
                continue

            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue

            response.raise_for_status()
            return response.json()

        raise RuntimeError(f"Échec Spotify après plusieurs tentatives : {url}")


def has_release_in_2026(spotify, artist_id):
    url = f"{SPOTIFY_API}/artists/{artist_id}/albums"
    params = {
        "include_groups": "album,single",
        "market": "FR",
        "limit": 50,
    }

    releases = []

    while True:
        data = spotify.get(url, params=params)

        for item in data.get("items", []):
            release_date = item.get("release_date", "")
            if release_date.startswith(str(RELEASE_YEAR)):
                releases.append(
                    {
                        "name": item.get("name"),
                        "type": item.get("album_type"),
                        "release_date": release_date,
                        "url": item.get("external_urls", {}).get("spotify"),
                    }
                )

        if releases:
            break

        next_url = data.get("next")
        if not next_url:
            break

        url = next_url
        params = None

    return releases


def get_top_track_name(spotify, artist_id):
    data = spotify.get(
        f"{SPOTIFY_API}/artists/{artist_id}/top-tracks",
        params={"market": "FR"},
    )

    tracks = data.get("tracks", [])
    return tracks[0]["name"] if tracks else None


def detect_french_from_lyrics(track_name, artist_name):
    try:
        response = requests.get(
            LRCLIB_API,
            params={
                "track_name": track_name,
                "artist_name": artist_name,
            },
            timeout=20,
        )

        if response.status_code != 200:
            return "undetermined", None

        results = response.json()

        for result in results:
            lyrics = result.get("plainLyrics") or result.get("syncedLyrics")

            if not lyrics:
                continue

            sample = lyrics[:2000]

            try:
                language = detect(sample)
            except LangDetectException:
                language = "unknown"

            french_score = len(FRENCH_MARKERS.findall(sample))

            if language == "fr" or french_score >= 4:
                return "french", {
                    "language": language,
                    "french_markers": french_score,
                    "source": "LRCLIB",
                }

            return "not_french", {
                "language": language,
                "french_markers": french_score,
                "source": "LRCLIB",
            }

        return "undetermined", None

    except requests.RequestException:
        return "undetermined", None


def main():
    artists = load_artists()
    state = load_json(STATE_FILE, {"processed": {}, "active": {}, "french": {}})

    active_artists = load_json(ACTIVE_FILE, {})
    french_artists = load_json(FRENCH_FILE, {})
    report = load_json(
        REPORT_FILE,
        {"inactive_2026": {}, "not_french": {}, "undetermined": {}},
    )

    spotify = SpotifyClient()

    candidates = [
        artist
        for artist in artists.values()
        if artist["id"] not in state["processed"]
        and (artist.get("monthly_listeners") or 0) >= MIN_LISTENERS
    ]

    candidates.sort(
        key=lambda artist: artist.get("monthly_listeners") or 0,
        reverse=True,
    )

    if MAX_ARTISTS > 0:
        candidates = candidates[:MAX_ARTISTS]

    print(f"Artistes à traiter dans cette exécution : {len(candidates)}")

    for index, artist in enumerate(candidates, start=1):
        artist_id = artist["id"]
        name = artist.get("name", "Artiste inconnu")

        print(f"[{index}/{len(candidates)}] {name}")

        try:
            releases = has_release_in_2026(spotify, artist_id)
        except Exception as error:
            print(f"Erreur Spotify pour {name} : {error}")
            state["processed"][artist_id] = "error"
            continue

        if not releases:
            report["inactive_2026"][artist_id] = {
                "name": name,
                "reason": "Aucune sortie album ou single en 2026",
            }
            state["processed"][artist_id] = "inactive_2026"
            continue

        artist_copy = dict(artist)
        artist_copy["releases_2026"] = releases[:10]
        active_artists[artist_id] = artist_copy
        state["active"][artist_id] = True

        track_name = get_top_track_name(spotify, artist_id)

        if not track_name:
            report["undetermined"][artist_id] = {
                "name": name,
                "reason": "Aucun titre populaire trouvé pour vérifier la langue",
            }
            state["processed"][artist_id] = "undetermined"
            continue

        verdict, details = detect_french_from_lyrics(track_name, name)

        if verdict == "french":
            artist_copy["french_check"] = details
            french_artists[artist_id] = artist_copy
            state["french"][artist_id] = True
            state["processed"][artist_id] = "french"
        elif verdict == "not_french":
            report["not_french"][artist_id] = {
                "name": name,
                "reason": "Paroles principales non détectées comme françaises",
                "details": details,
            }
            state["processed"][artist_id] = "not_french"
        else:
            report["undetermined"][artist_id] = {
                "name": name,
                "reason": "Langue non confirmée",
                "details": details,
            }
            state["processed"][artist_id] = "undetermined"

        if index % 25 == 0:
            save_json(STATE_FILE, state)
            save_json(ACTIVE_FILE, active_artists)
            save_json(FRENCH_FILE, french_artists)
            save_json(REPORT_FILE, report)

        time.sleep(0.3)

    save_json(STATE_FILE, state)
    save_json(ACTIVE_FILE, active_artists)
    save_json(FRENCH_FILE, french_artists)
    save_json(REPORT_FILE, report)

    print("Traitement terminé.")
    print(f"Artistes actifs en 2026 : {len(active_artists)}")
    print(f"Artistes francophones conservés : {len(french_artists)}")
    print(f"Exclusions pour inactivité 2026 : {len(report['inactive_2026'])}")
    print(f"Exclusions pour non-francophonie : {len(report['not_french'])}")
    print(f"Cas indéterminés : {len(report['undetermined'])}")


if __name__ == "__main__":
    main()
