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

DEEZER_API = "https://api.deezer.com"
LRCLIB_API = "https://lrclib.net/api/search"

RELEASE_YEAR = 2026
MIN_LISTENERS = int(os.getenv("MIN_MONTHLY_LISTENERS", "0"))
MAX_ARTISTS = int(os.getenv("MAX_ARTISTS", "500"))

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

        with path.open(encoding="utf-8") as file:
            content = json.load(file)

        for artist_id, artist in content.get("artists", {}).items():
            artist["id"] = artist.get("id", artist_id)
            artists[artist_id] = artist

    print(f"Artistes uniques chargés : {len(artists)}")
    return artists


def load_json(path, default):
    if path.exists():
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    return default


def save_json(path, data):
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def deezer_get(url, params=None):
    for attempt in range(5):
        try:
            response = requests.get(url, params=params, timeout=30)

            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", "5"))
                print(f"Rate limit Deezer : attente de {wait} secondes")
                time.sleep(wait)
                continue

            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue

            response.raise_for_status()
            return response.json()

        except requests.RequestException as error:
            print(f"Erreur réseau Deezer : {error}")
            time.sleep(2 ** attempt)

    raise RuntimeError(f"Échec Deezer après plusieurs tentatives : {url}")


def normalize_name(value):
    return re.sub(r"\s+", " ", value or "").strip().lower()


def find_deezer_artist(artist_name):
    data = deezer_get(
        f"{DEEZER_API}/search/artist",
        params={"q": artist_name, "limit": 5},
    )

    results = data.get("data", [])

    if not results:
        return None

    wanted = normalize_name(artist_name)

    exact = [
        result
        for result in results
        if normalize_name(result.get("name")) == wanted
    ]

    if exact:
        return exact[0]

    return results[0]


def has_release_in_2026(deezer_artist_id):
    url = f"{DEEZER_API}/artist/{deezer_artist_id}/albums"
    releases = []
    index = 0

    while True:
        data = deezer_get(url, params={"limit": 100, "index": index})
        items = data.get("data", [])

        for album in items:
            release_date = album.get("release_date", "")

            if release_date.startswith(str(RELEASE_YEAR)):
                releases.append(
                    {
                        "name": album.get("title"),
                        "type": album.get("album_type") or album.get("type"),
                        "release_date": release_date,
                        "url": album.get("link"),
                    }
                )

        if releases:
            return releases

        index += len(items)

        if not items or index >= data.get("total", 0):
            return []


def get_popular_track(deezer_artist_id):
    data = deezer_get(f"{DEEZER_API}/artist/{deezer_artist_id}/top", params={"limit": 1})
    tracks = data.get("data", [])
    return tracks[0] if tracks else None


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
        {
            "artist_not_found": {},
            "inactive_2026": {},
            "not_french": {},
            "undetermined": {},
        },
    )

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
            deezer_artist = find_deezer_artist(name)
        except Exception as error:
            print(f"Erreur Deezer pour {name} : {error}")
            state["processed"][artist_id] = "error"
            continue

        if not deezer_artist:
            report["artist_not_found"][artist_id] = {
                "name": name,
                "reason": "Artiste introuvable sur Deezer",
            }
            state["processed"][artist_id] = "artist_not_found"
            continue

        deezer_id = deezer_artist["id"]
        deezer_name = deezer_artist.get("name", name)

        try:
            releases = has_release_in_2026(deezer_id)
        except Exception as error:
            print(f"Erreur albums pour {name} : {error}")
            state["processed"][artist_id] = "error"
            continue

        if not releases:
            report["inactive_2026"][artist_id] = {
                "name": name,
                "deezer_name": deezer_name,
                "deezer_id": deezer_id,
                "reason": "Aucune sortie album ou single en 2026",
            }
            state["processed"][artist_id] = "inactive_2026"
            continue

        artist_copy = dict(artist)
        artist_copy["deezer_id"] = deezer_id
        artist_copy["deezer_name"] = deezer_name
        artist_copy["releases_2026"] = releases[:10]
        active_artists[artist_id] = artist_copy
        state["active"][artist_id] = True

        track = get_popular_track(deezer_id)

        if not track:
            report["undetermined"][artist_id] = {
                "name": name,
                "reason": "Aucun titre populaire trouvé pour vérifier la langue",
            }
            state["processed"][artist_id] = "undetermined"
            continue

        track_name = track.get("title")
        verdict, details = detect_french_from_lyrics(track_name, deezer_name)

        if verdict == "french":
            artist_copy["french_check"] = details
            artist_copy["checked_track"] = track_name
            french_artists[artist_id] = artist_copy
            state["french"][artist_id] = True
            state["processed"][artist_id] = "french"
        elif verdict == "not_french":
            report["not_french"][artist_id] = {
                "name": name,
                "deezer_name": deezer_name,
                "checked_track": track_name,
                "reason": "Paroles principales non détectées comme françaises",
                "details": details,
            }
            state["processed"][artist_id] = "not_french"
        else:
            report["undetermined"][artist_id] = {
                "name": name,
                "deezer_name": deezer_name,
                "checked_track": track_name,
                "reason": "Langue non confirmée",
                "details": details,
            }
            state["processed"][artist_id] = "undetermined"

        if index % 25 == 0:
            save_json(STATE_FILE, state)
            save_json(ACTIVE_FILE, active_artists)
            save_json(FRENCH_FILE, french_artists)
            save_json(REPORT_FILE, report)

        time.sleep(0.4)

    save_json(STATE_FILE, state)
    save_json(ACTIVE_FILE, active_artists)
    save_json(FRENCH_FILE, french_artists)
    save_json(REPORT_FILE, report)

    print("Traitement terminé.")
    print(f"Artistes actifs en 2026 : {len(active_artists)}")
    print(f"Artistes francophones conservés : {len(french_artists)}")
    print(f"Artistes introuvables : {len(report['artist_not_found'])}")
    print(f"Exclusions pour inactivité 2026 : {len(report['inactive_2026'])}")
    print(f"Exclusions pour non-francophonie : {len(report['not_french'])}")
    print(f"Cas indéterminés : {len(report['undetermined'])}")


if __name__ == "__main__":
    main()
