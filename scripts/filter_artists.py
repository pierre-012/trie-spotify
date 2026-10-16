import json
import os
import re
import time
from pathlib import Path

import requests
from langdetect import DetectorFactory, detect, LangDetectException

DetectorFactory.seed = 0

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
TRACK_LIMIT = 100
LYRICS_CHECK_LIMIT = 5

STRONG_FRENCH_RATIO = 0.6
BORDERLINE_FRENCH_RATIO = 0.3

MIN_LISTENERS = int(os.getenv("MIN_MONTHLY_LISTENERS", "0"))
MAX_ARTISTS = int(os.getenv("MAX_ARTISTS", "25"))

FRENCH_MARKERS = re.compile(
    r"\b(je|tu|il|elle|nous|vous|ils|elles|mais|donc|alors|avec|sans|pour|dans|sur|"
    r"parce|quand|jamais|toujours|encore|maintenant|c'est|j'ai|t'es|on est|ça|ça va)\b",
    re.IGNORECASE,
)

FRENCH_COMMON_WORDS = re.compile(
    r"\b(la|le|les|un|une|des|du|de|et|ou|est|sont|mon|ma|mes|ton|ta|tes|son|sa|ses|"
    r"notre|nos|votre|vos|leur|leurs|ce|cet|cette|ces|qui|que|quoi|où|comment|"
    r"aimer|amour|cœur|coeur|vie|nuit|jour|soleil|lune|ciel|rue|ville|monde|"
    r"bande|organisée|organise|gang|caillera|biff|wesh|gros|bébé|bebe|chérie|cherie)\b",
    re.IGNORECASE,
)

NON_FRENCH_LANGUAGES = {
    "en", "es", "it", "de", "pt", "nl", "ar", "ru", "tr", "pl", "sv", "da", "no",
}


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
    last_error = None

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
            last_error = error
            time.sleep(2 ** attempt)

    raise RuntimeError(f"Échec Deezer après plusieurs tentatives : {url} ({last_error})")


def normalize_name(value):
    return re.sub(r"\s+", " ", value or "").strip().lower()


def clean_track_name(track_name):
    name = track_name or ""

    name = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", name)
    name = re.sub(r"\s*(feat|ft|with)\.?\s+.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()

    return name


def find_deezer_artist(artist_name):
    data = deezer_get(
        f"{DEEZER_API}/search/artist",
        params={"q": artist_name, "limit": 5},
    )

    results = data.get("data", [])

    if not results:
        return None

    wanted = normalize_name(artist_name)

    for result in results:
        if normalize_name(result.get("name")) == wanted:
            return result

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


def get_artist_tracks(deezer_artist_id):
    data = deezer_get(
        f"{DEEZER_API}/artist/{deezer_artist_id}/top",
        params={"limit": TRACK_LIMIT},
    )

    return data.get("data", [])


def is_french_title(track_name):
    cleaned = clean_track_name(track_name)

    if not cleaned or len(cleaned) < 3:
        return None

    french_score = len(FRENCH_MARKERS.findall(cleaned))
    common_score = len(FRENCH_COMMON_WORDS.findall(cleaned))

    has_french_diacritics = bool(
        re.search(r"[àâäéèêëîïôöùûüçœ]", cleaned, re.IGNORECASE)
    )

    try:
        language = detect(cleaned)
    except LangDetectException:
        language = "unknown"

    if language == "fr" or french_score >= 1 or common_score >= 2 or has_french_diacritics:
        return True

    if language in NON_FRENCH_LANGUAGES and french_score == 0 and common_score == 0:
        return False

    return None


def analyze_titles(tracks):
    french_count = 0
    analyzed_count = 0
    analyzed_titles = []

    for track in tracks:
        title = track.get("title")

        if not title:
            continue

        verdict = is_french_title(title)

        if verdict is None:
            continue

        analyzed_count += 1
        analyzed_titles.append(
            {
                "title": title,
                "french": verdict,
            }
        )

        if verdict:
            french_count += 1

    if analyzed_count == 0:
        return "not_french", {
            "analyzed_tracks": 0,
            "french_tracks": 0,
            "ratio": 0,
            "reason": "Aucun titre analysable",
            "tracks": [],
        }

    ratio = french_count / analyzed_count

    details = {
        "analyzed_tracks": analyzed_count,
        "french_tracks": french_count,
        "ratio": round(ratio, 3),
        "tracks": analyzed_titles,
    }

    print(
        f"    Titres analysés : {analyzed_count} | "
        f"français : {french_count} | ratio : {ratio:.2f}"
    )

    if ratio > STRONG_FRENCH_RATIO:
        return "french", details

    if ratio < BORDERLINE_FRENCH_RATIO:
        return "not_french", details

    return "borderline", details


def get_lyrics(track_name, artist_name):
    cleaned_track = clean_track_name(track_name)

    queries = [
        {
            "track_name": cleaned_track,
            "artist_name": artist_name,
        },
        {
            "q": f"{artist_name} {cleaned_track}",
        },
    ]

    for params in queries:
        try:
            response = requests.get(LRCLIB_API, params=params, timeout=20)

            if response.status_code != 200:
                continue

            results = response.json()

            for result in results:
                if result.get("instrumental"):
                    continue

                lyrics = result.get("plainLyrics") or result.get("syncedLyrics")

                if lyrics:
                    return lyrics

        except requests.RequestException:
            continue

    return None


def analyze_lyrics(lyrics):
    sample = lyrics[:2500]

    try:
        language = detect(sample)
    except LangDetectException:
        language = "unknown"

    french_score = len(FRENCH_MARKERS.findall(sample))
    common_score = len(FRENCH_COMMON_WORDS.findall(sample))

    details = {
        "language": language,
        "french_markers": french_score,
        "common_words": common_score,
        "source": "LRCLIB",
    }

    if language == "fr" or french_score >= 4 or common_score >= 6:
        return "french", details

    return "not_french", details


def confirm_french_with_lyrics(tracks, artist_name):
    checked_tracks = []
    french_lyrics_count = 0
    analyzed_lyrics_count = 0

    for track in tracks[:LYRICS_CHECK_LIMIT]:
        title = track.get("title")

        if not title:
            continue

        lyrics = get_lyrics(title, artist_name)

        if not lyrics:
            continue

        verdict, details = analyze_lyrics(lyrics)

        analyzed_lyrics_count += 1
        checked_tracks.append(
            {
                "title": title,
                "verdict": verdict,
                "details": details,
            }
        )

        if verdict == "french":
            french_lyrics_count += 1
            break

        time.sleep(0.2)

    details = {
        "method": "title_ratio_plus_lyrics_confirmation",
        "checked_tracks": checked_tracks,
        "analyzed_lyrics": analyzed_lyrics_count,
        "french_lyrics": french_lyrics_count,
    }

    if french_lyrics_count > 0:
        return "french", details

    return "not_french", details


def main():
    artists = load_artists()

    state = load_json(
        STATE_FILE,
        {
            "processed": {},
            "active": {},
            "french": {},
        },
    )

    active_artists = load_json(ACTIVE_FILE, {})
    french_artists = load_json(FRENCH_FILE, {})

    report = load_json(
        REPORT_FILE,
        {
            "artist_not_found": {},
            "inactive_2026": {},
            "not_french": {},
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

        try:
            tracks = get_artist_tracks(deezer_id)
        except Exception as error:
            print(f"Erreur titres pour {name} : {error}")
            state["processed"][artist_id] = "error"
            continue

        if not tracks:
            report["not_french"][artist_id] = {
                "name": name,
                "deezer_name": deezer_name,
                "reason": "Aucun titre trouvé sur Deezer",
            }
            state["processed"][artist_id] = "not_french"
            continue

        verdict, title_details = analyze_titles(tracks)
        artist_copy["title_check"] = title_details

        if verdict == "borderline":
            print("    Ratio intermédiaire : vérification des paroles")
            verdict, lyrics_details = confirm_french_with_lyrics(
                tracks,
                deezer_name,
            )
            artist_copy["lyrics_check"] = lyrics_details

        if verdict == "french":
            french_artists[artist_id] = artist_copy
            state["french"][artist_id] = True
            state["processed"][artist_id] = "french"

        else:
            report["not_french"][artist_id] = {
                "name": name,
                "deezer_name": deezer_name,
                "reason": "Francophonie non confirmée",
                "title_check": title_details,
                "lyrics_check": artist_copy.get("lyrics_check"),
            }
            state["processed"][artist_id] = "not_french"

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


if __name__ == "__main__":
    main()
