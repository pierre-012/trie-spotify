import functools
import json
import os
import re
import time
from datetime import datetime
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
LYRICS_CHECK_LIMIT = 10

STRONG_FRENCH_RATIO = 0.6
LYRICS_FRENCH_RATIO = 0.5

MIN_LISTENERS = int(os.getenv("MIN_MONTHLY_LISTENERS", "0"))
MAX_ARTISTS = int(os.getenv("MAX_ARTISTS", "0"))

MAX_RUN_SECONDS = int(os.getenv("MAX_RUN_SECONDS", "17100"))
SAFETY_MARGIN_SECONDS = 300

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


def log(*values):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}]", *values, flush=True)


def load_artists():
    artists = {}

    for path in SOURCE_FILES:
        if not path.exists():
            log(f"Fichier absent, ignoré : {path}")
            continue

        with path.open(encoding="utf-8") as file:
            content = json.load(file)

        for artist_id, artist in content.get("artists", {}).items():
            artist["id"] = artist.get("id", artist_id)
            artists[artist_id] = artist

    log(f"Artistes uniques chargés : {len(artists)}")
    return artists


def load_json(path, default):
    """
    Charge un fichier JSON de manière sécurisée.

    - Fichier absent ou vide : utilise la valeur par défaut.
    - JSON invalide : signale le problème et utilise la valeur par défaut.
    - Structure inattendue : utilise la valeur par défaut.
    """
    if not path.exists():
        log(f"Fichier absent, initialisation : {path.name}")
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            content = file.read().strip()

        if not content:
            log(f"Fichier vide, initialisation : {path.name}")
            return default

        data = json.loads(content)

        # Vérifie que le JSON possède le type attendu.
        if not isinstance(data, type(default)):
            log(f"Format invalide, réinitialisation : {path.name}")
            return default

        # Vérifie la structure du fichier de progression.
        if path == STATE_FILE:
            required_keys = {"processed", "active", "french"}

            if not required_keys.issubset(data.keys()):
                log(f"Structure de progression invalide : {path.name}")
                return default

            if not all(
                isinstance(data[key], dict)
                for key in required_keys
            ):
                log(f"Données de progression invalides : {path.name}")
                return default

        return data

    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        log(f"JSON illisible : {path.name} ({error})")
        log("Utilisation des valeurs par défaut.")
        return default

    except OSError as error:
        log(f"Impossible de lire {path.name} : {error}")
        raise
        
# Champs conservés dans les fichiers finaux d'artistes.
# Les données Deezer et les résultats d'analyse restent utilisables en mémoire,
# mais ne sont pas enregistrés dans artistes_actifs_2026.json ou
# artistes_francophones_2026.json.
ARTIST_FIELDS = {
    "id",
    "name",
    "url",
    "uri",
    "monthly_listeners",
    "genres",
    "external_urls",
}


def clean_artist_data(artists):
    """Ne conserve que les champs utiles dans les fichiers d'artistes."""
    cleaned = {}

    for artist_id, artist in artists.items():
        cleaned[artist_id] = {
            key: value
            for key, value in artist.items()
            if key in ARTIST_FIELDS
        }

    return cleaned


def save_json(path, data, compact=False):
    """Sauvegarde un JSON de façon atomique, avec option de compression."""
    temp_path = path.with_suffix(".tmp")

    with temp_path.open("w", encoding="utf-8") as file:
        if compact:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            json.dump(data, file, ensure_ascii=False, indent=2)

    temp_path.replace(path)


def save_all(state, active_artists, french_artists, report):
    save_json(STATE_FILE, state)
    save_json(ACTIVE_FILE, clean_artist_data(active_artists), compact=True)
    save_json(FRENCH_FILE, clean_artist_data(french_artists), compact=True)
    save_json(REPORT_FILE, report)


def deezer_get(url, params=None):
    last_error = None

    for attempt in range(5):
        try:
            response = requests.get(url, params=params, timeout=30)

            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", "5"))
                log(f"Rate limit Deezer : attente de {wait} secondes")
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
            log(f"    Sortie 2026 trouvée : {releases[0]['name']} ({releases[0]['release_date']})")
            return releases

        index += len(items)

        if not items or index >= data.get("total", 0):
            return []


def get_artist_tracks(deezer_artist_id):
    data = deezer_get(
        f"{DEEZER_API}/artist/{deezer_artist_id}/top",
        params={"limit": TRACK_LIMIT},
    )

    tracks = data.get("data", [])

    if tracks:
        log(f"    Titres populaires récupérés : {len(tracks)}")
        return tracks

    log("    Aucun titre populaire : fallback sur les albums")

    albums_data = deezer_get(
        f"{DEEZER_API}/artist/{deezer_artist_id}/albums",
        params={"limit": 25, "index": 0},
    )

    albums = albums_data.get("data", [])
    tracks = []

    for album in albums:
        album_id = album.get("id")

        if not album_id:
            continue

        album_tracks_data = deezer_get(
            f"{DEEZER_API}/album/{album_id}/tracks",
            params={"limit": 100},
        )

        album_tracks = album_tracks_data.get("data", [])

        for track in album_tracks:
            track["album_title"] = album.get("title")
            tracks.append(track)

        if len(tracks) >= TRACK_LIMIT:
            break

        time.sleep(0.2)

    tracks = tracks[:TRACK_LIMIT]

    log(f"    Titres récupérés via albums : {len(tracks)}")
    return tracks


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

    ratio = french_count / analyzed_count if analyzed_count else 0

    details = {
        "analyzed_tracks": analyzed_count,
        "french_tracks": french_count,
        "ratio": round(ratio, 3),
        "tracks": analyzed_titles,
    }

    log(
        f"    Titres analysés : {analyzed_count} | "
        f"français : {french_count} | ratio : {ratio:.2f}"
    )

    return details


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

    if language == "fr" or french_score >= 3 or common_score >= 5:
        return "french", details

    return "not_french", details


def confirm_french_with_lyrics(tracks, artist_name):
    checked_tracks = []
    french_count = 0
    analyzed_count = 0

    log("    Vérification des paroles via LRCLIB")

    for track in tracks:
        if analyzed_count >= LYRICS_CHECK_LIMIT:
            break

        title = track.get("title")

        if not title:
            continue

        lyrics = get_lyrics(title, artist_name)

        if not lyrics:
            log(f"    Paroles introuvables : {title}")
            continue

        verdict, details = analyze_lyrics(lyrics)

        analyzed_count += 1
        checked_tracks.append(
            {
                "title": title,
                "verdict": verdict,
                "details": details,
            }
        )

        log(
            f"    Paroles analysées : {title} | "
            f"verdict : {verdict} | langue : {details['language']}"
        )

        if verdict == "french":
            french_count += 1

        time.sleep(0.2)

    ratio = french_count / analyzed_count if analyzed_count else 0

    details = {
        "method": "lyrics_confirmation",
        "checked_tracks": checked_tracks,
        "analyzed_lyrics": analyzed_count,
        "french_lyrics": french_count,
        "ratio": round(ratio, 3),
    }

    log(
        f"    Bilan paroles : {analyzed_count} analysées | "
        f"{french_count} françaises | ratio : {ratio:.2f}"
    )

    if analyzed_count == 0:
        return "not_french", details

    if ratio > LYRICS_FRENCH_RATIO:
        return "french", details

    return "not_french", details


def process_artist(artist, state, active_artists, french_artists, report):
    artist_id = artist["id"]
    name = artist.get("name", "Artiste inconnu")

    deezer_artist = find_deezer_artist(name)

    if not deezer_artist:
        log("    Artiste introuvable sur Deezer")

        report["artist_not_found"][artist_id] = {
            "name": name,
            "reason": "Artiste introuvable sur Deezer",
        }
        state["processed"][artist_id] = "artist_not_found"
        return

    deezer_id = deezer_artist["id"]
    deezer_name = deezer_artist.get("name", name)

    log(f"    Correspondance Deezer : {deezer_name} (ID {deezer_id})")

    releases = has_release_in_2026(deezer_id)

    if not releases:
        log("    Aucune sortie album ou single en 2026 : exclu")

        report["inactive_2026"][artist_id] = {
            "name": name,
            "deezer_name": deezer_name,
            "deezer_id": deezer_id,
            "reason": "Aucune sortie album ou single en 2026",
        }
        state["processed"][artist_id] = "inactive_2026"
        return

    artist_copy = dict(artist)
    artist_copy["deezer_id"] = deezer_id
    artist_copy["deezer_name"] = deezer_name
    artist_copy["releases_2026"] = releases[:10]

    active_artists[artist_id] = artist_copy
    state["active"][artist_id] = True

    tracks = get_artist_tracks(deezer_id)

    if not tracks:
        log("    Aucun titre récupérable, même via les albums : exclu")

        report["not_french"][artist_id] = {
            "name": name,
            "deezer_name": deezer_name,
            "deezer_id": deezer_id,
            "reason": "Aucun titre trouvé sur Deezer",
        }
        state["processed"][artist_id] = "not_french"
        return

    title_details = analyze_titles(tracks)
    artist_copy["title_check"] = title_details

    title_ratio = title_details["ratio"]

    if title_ratio > STRONG_FRENCH_RATIO:
        verdict = "french"
        log("    Ratio de titres suffisant : conservé")

        artist_copy["french_check"] = {
            "method": "track_titles",
            **title_details,
        }

    else:
        log("    Ratio insuffisant : vérification des paroles")

        verdict, lyrics_details = confirm_french_with_lyrics(
            tracks,
            deezer_name,
        )

        artist_copy["french_check"] = {
            "method": "lyrics_confirmation",
            "title_check": title_details,
            **lyrics_details,
        }

    if verdict == "french":
        french_artists[artist_id] = artist_copy
        state["french"][artist_id] = True
        state["processed"][artist_id] = "french"

        log("    Résultat : FRANCOPHONE ET ACTIF, conservé")

    else:
        report["not_french"][artist_id] = {
            "name": name,
            "deezer_name": deezer_name,
            "reason": "Francophonie non confirmée",
            "french_check": artist_copy.get("french_check"),
        }
        state["processed"][artist_id] = "not_french"

        log("    Résultat : non conservé")


def main():
    started_at = time.time()
    deadline = started_at + MAX_RUN_SECONDS - SAFETY_MARGIN_SECONDS

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

    log(f"Artistes à traiter dans cette exécution : {len(candidates)}")
    log(
        f"Arrêt prévu après {MAX_RUN_SECONDS // 60} minutes, "
        f"avec {SAFETY_MARGIN_SECONDS // 60} minutes de marge"
    )

    processed_in_run = 0

    for index, artist in enumerate(candidates, start=1):
        if time.time() >= deadline:
            log("Limite de temps atteinte : arrêt propre du traitement.")
            break

        artist_id = artist["id"]
        name = artist.get("name", "Artiste inconnu")

        log(f"[{index}/{len(candidates)}] {name}")

        try:
            process_artist(artist, state, active_artists, french_artists, report)
            processed_in_run += 1

        except Exception as error:
            log(f"Erreur non bloquante pour {name} : {error}")
            state["processed"][artist_id] = "error"

        save_all(state, active_artists, french_artists, report)

        time.sleep(0.25)

    save_all(state, active_artists, french_artists, report)

    elapsed = int(time.time() - started_at)
    remaining = len(artists) - len(state["processed"])

    log("Traitement terminé pour cette exécution.")
    log(f"Artistes traités dans ce run : {processed_in_run}")
    log(f"Durée : {elapsed // 60} minutes")
    log(f"Artistes restants : {remaining}")
    log(f"Artistes actifs en 2026 : {len(active_artists)}")
    log(f"Artistes francophones conservés : {len(french_artists)}")
    log(f"Artistes introuvables : {len(report['artist_not_found'])}")
    log(f"Exclusions pour inactivité 2026 : {len(report['inactive_2026'])}")
    log(f"Exclusions pour non-francophonie : {len(report['not_french'])}")


if __name__ == "__main__":
    main()
