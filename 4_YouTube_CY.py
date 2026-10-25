# Dateiname: 4_YouTube_CY_v2.py
# Voraussetzungen: pip install yt-dlp youtube-transcript-api requests python-dotenv

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yt_dlp
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi

# ==========================================
# 0. Initialisierung & Konfiguration
# ==========================================

# Logging konfigurieren (inkl. Zeitstempel für Cronjob-Logs)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Umgebungsvariablen laden (Cronjob-sicher über pathlib)
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
load_dotenv(ENV_PATH)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([OPENROUTER_API_KEY, BOT_TOKEN, CHAT_ID]):
    raise ValueError(
        f"Fehlende API-Keys! Konnte sie nicht aus {ENV_PATH} laden. "
        "Bitte prüfe die .env-Datei."
    )

# Allgemeine Einstellungen (Angepasst für Cycle / FSCtv)
TARGET_URL = "https://www.youtube.com/@FSCtv/videos"
MD_DIR = Path("/home/freddy/Dokumente/Obsidian_YT_Cycle")
AI_MODEL = "nvidia/nemotron-3.5-lightning:free"
LOCAL_TZ = ZoneInfo("Europe/Berlin")

# Maximales Alter einer Video-Liste in Stunden, bevor fehlende Transkripte
# ignoriert werden (verhindert Endlosschleifen bei Videos ohne Untertitel)
MAX_VIDEO_FILE_AGE_HOURS = 48

# Historien-Dateien (Namensschema für Cycle)
HISTORY_FILE_VIDEOS = MD_DIR / "Cycle_verarbeitete_videos.txt"
HISTORY_FILE_TRANSCRIPTS = MD_DIR / "Cycle_geladene_transkripte.txt"
HISTORY_FILE_SENT = MD_DIR / "Cycle_versendete_zusammenfassungen.txt"

# Stelle sicher, dass das Zielverzeichnis existiert
MD_DIR.mkdir(parents=True, exist_ok=True)


# ==========================================
# Hilfsfunktionen (Allgemein)
# ==========================================

def load_history(filepath: Path) -> set[str]:
    """Lädt die Liste bereits verarbeiteter Video-IDs."""
    if not filepath.exists():
        return set()
    # utf-8-sig beim Lesen akzeptiert sowohl altes BOM als auch reines UTF-8
    with filepath.open("r", encoding="utf-8-sig") as f:
        return {line.strip() for line in f if line.strip()}


def append_to_history(filepath: Path, item_id: str) -> None:
    """Speichert eine ID in die jeweilige Historie (sauberes UTF-8)."""
    with filepath.open("a", encoding="utf-8") as f:
        f.write(f"{item_id}
")


def mark_file_as_done(filepath: Path) -> None:
    """Benennt eine abgearbeitete Markdown-Datei in 'erledigt_...' um."""
    done_path = filepath.with_name(f"erledigt_{filepath.name}")
    try:
        filepath.rename(done_path)
        logging.info(f"Datei als erledigt markiert: {done_path.name}")
    except OSError as e:
        logging.error(f"Konnte Datei {filepath.name} nicht umbenennen: {e}")


def is_file_older_than(filepath: Path, hours: int) -> bool:
    """Prüft, ob eine Datei älter als X Stunden ist."""
    mtime = datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime) > timedelta(hours=hours)


# ==========================================
# PHASE 1: Links ermitteln
# ==========================================

def get_working_day_cutoff(hours: int = 24) -> datetime:
    """Berechnet den Cutoff-Zeitpunkt unter Berücksichtigung des Wochenendes."""
    now = datetime.now(timezone.utc)
    current_weekday = now.weekday()
    if current_weekday == 0:  # Montag -> Rückblick bis Freitag
        return now - timedelta(hours=hours + 48)
    elif current_weekday == 6:  # Sonntag -> Rückblick bis Freitag
        return now - timedelta(hours=hours + 24)
    return now - timedelta(hours=hours)


def fetch_new_videos(known_ids: set[str]) -> list[dict]:
    """Sucht auf dem Kanal nach neuen Videos seit dem letzten Arbeitstag."""
    cutoff_date = get_working_day_cutoff(hours=24)
    recent_videos = []

    # Schritt 1: Schneller Flat-Abruf (schont YouTube-Rate-Limits und spart Zeit)
    flat_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "playlistend": 15,
        "ignoreerrors": True
    }

    try:
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            logging.info(f"[Phase 1] Prüfe Kanal-Übersicht: {TARGET_URL}...")
            info = ydl.extract_info(TARGET_URL, download=False)
            entries = (info or {}).get("entries") or []

        # Filtere bereits bekannte Video-IDs sofort heraus
        unknown_entries = [
            e for e in entries
            if e and e.get("id") and e.get("id") not in known_ids
        ]

        if not unknown_entries:
            return []

        # Schritt 2: Detail-Abruf nur für wirklich unbekannte Videos
        detail_opts = {"quiet": True, "no_warnings": True, "ignoreerrors": True}
        with yt_dlp.YoutubeDL(detail_opts) as ydl:
            for entry in unknown_entries:
                video_id = entry["id"]
                video_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"

                try:
                    v_info = ydl.extract_info(video_url, download=False)
                    if not v_info:
                        continue
                except Exception as e:
                    logging.warning(f"[Phase 1] Konnte Details für {video_id} nicht laden: {e}")
                    continue

                # Ignoriere geplante Premieren/Streams, die noch nicht abgeschlossen sind
                live_status = v_info.get("live_status")
                if live_status in ("is_upcoming", "is_live"):
                    logging.info(f"[Phase 1] Überspringe aktives/geplantes Video {video_id} (Status: {live_status}).")
                    continue

                # Zeitstempel ermitteln
                ts = v_info.get("release_timestamp") or v_info.get("timestamp")
                video_date = None
                if ts:
                    video_date = datetime.fromtimestamp(ts, tz=timezone.utc)
                elif v_info.get("upload_date"):
                    try:
                        video_date = datetime.strptime(v_info["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass

                if not video_date:
                    continue

                if video_date >= cutoff_date:
                    duration_sec = v_info.get("duration")
                    duration_str = str(timedelta(seconds=int(duration_sec))) if duration_sec else "Unbekannt"
                    recent_videos.append({
                        "title": v_info.get("title", "Ohne Titel"),
                        "url": v_info.get("webpage_url", video_url),
                        "id": video_id,
                        "date": video_date.astimezone(LOCAL_TZ),  # Korrekte deutsche Zeitzone
                        "duration": duration_str
                    })

        return recent_videos
    except Exception as e:
        logging.error(f"[Phase 1] Fehler beim Abrufen der Videos: {e}")
        return []


def save_video_list_md(videos: list[dict]) -> None:
    """Speichert die gefundenen Videos in einer Markdown-Liste."""
    if not videos:
        return
    timestamp_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d_%H%M")
    filepath = MD_DIR / f"Cycle_videos_{timestamp_str}.md"
    try:
        with filepath.open("w", encoding="utf-8") as f:
            f.write("# Neue Videos: Cycle (Letzter Arbeitstag)\n\n")
            for v in videos:
                date_str = v["date"].strftime("%d.%m.%Y um %H:%M")
                f.write(f"### [{v['title']}]({v['url']})\n")
                f.write(f"- **Video-ID:** {v['id']}\n")
                f.write(f"- **Veröffentlicht am:** {date_str} Uhr\n")
                f.write(f"- **Dauer:** {v['duration']}\n\n")
        logging.info(f"[Phase 1] {len(videos)} neue(s) Video(s) in {filepath.name} gespeichert.")
    except Exception as e:
        logging.error(f"[Phase 1] Fehler beim Schreiben der Video-Liste: {e}")


# ==========================================
# PHASE 2: Transkripte laden
# ==========================================

def parse_video_md_file(filepath: Path) -> list[dict]:
    """Liest die Video-Metadaten aus einer Video-Markdown-Datei aus."""
    videos = []
    with filepath.open("r", encoding="utf-8-sig") as f:
        content = f.read()

    # Robuster Regex: Unterstützt auch eckige Klammern innerhalb des Videotitels
    pattern = (
        r"### \[(.*)\]\((https?://[^\s)]+)\)\s*
"
        r"- \*\*Video-ID:\*\* (.*?)\s*
"
        r"- \*\*Veröffentlicht am:\*\* (.*?)(?:
|$)"
    )
    for match in re.finditer(pattern, content):
        videos.append({
            "title": match.group(1).strip(),
            "url": match.group(2).strip(),
            "id": match.group(3).strip(),
            "date": match.group(4).strip()
        })
    return videos


def fetch_transcript(video_id: str) -> str | None:
    """Lädt das deutsche oder englische Transkript zu einer Video-ID herunter."""
    try:
        logging.info(f"[Phase 2] Lade Transkript für {video_id}...")
        # Kompatibel mit youtube-transcript-api (statisch & Objekt-API)
        transcript = None
        try:
            if hasattr(YouTubeTranscriptApi, "get_transcript"):
                transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=["de"])
            else:
                transcript = YouTubeTranscriptApi().fetch(video_id, languages=["de"])
        except Exception:
            logging.info(f"[Phase 2] Kein DE-Transkript für {video_id}, versuche EN...")
            if hasattr(YouTubeTranscriptApi, "get_transcript"):
                transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=["en"])
            else:
                transcript = YouTubeTranscriptApi().fetch(video_id, languages=["en"])

        lines = []
        for entry in transcript:
            # Unterstützt sowohl Dicts (Standard) als auch FetchedTranscriptSnippet-Objekte
            start = entry["start"] if isinstance(entry, dict) else entry.start
            text = entry["text"] if isinstance(entry, dict) else entry.text
            # Zeilenumbrüche innerhalb eines Snippets bereinigen
            clean_text = text.replace("\n", " ").strip()
            lines.append(f"[{int(start // 60):02d}:{int(start % 60):02d}] {clean_text}")

        return "\n".join(lines)
    except Exception as e:
        logging.warning(f"[Phase 2] Transkript für {video_id} nicht verfügbar: {e}")
        return None


def save_transcript_md(video_info: dict, transcript_text: str) -> bool:
    """Speichert das Roh-Transkript als Markdown-Datei."""
    timestamp_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d_%H%M")
    filename = f"Cycle_Transkript_{timestamp_str}_{video_info['id']}.md"
    filepath = MD_DIR / filename
    try:
        with filepath.open("w", encoding="utf-8") as f:
            f.write(f"# Roh-Transkript: {video_info['title']}\n\n")
            f.write(f"- **Video-ID:** {video_info['id']}\n")
            f.write(f"- **Veröffentlicht am:** {video_info['date']}\n")
            f.write(f"- **Link zum Video:** {video_info['url']}\n\n---\n\n")
            f.write(f"## Transkript-Text\n\n{transcript_text}")
        return True
    except Exception as e:
        logging.error(f"[Phase 2] Speicherfehler bei Transkript {video_info['id']}: {e}")
        return False


# ==========================================
# PHASE 3: Zusammenfassen & Senden
# ==========================================

def parse_transcript_md(filepath: Path) -> dict | None:
    """Liest Metadaten und Transkript-Text aus einer Transkript-Datei."""
    with filepath.open("r", encoding="utf-8-sig") as f:
        content = f.read()

    title_match = re.search(r"# Roh-Transkript: (.*)", content)
    id_match = re.search(r"- \*\*Video-ID:\*\* (.*)", content)
    date_match = re.search(r"- \*\*Veröffentlicht am:\*\* (.*)", content)
    url_match = re.search(r"- \*\*Link zum Video:\*\* (.*)", content)
    transcript_match = re.search(r"## Transkript-Text\s*

(.*)", content, re.DOTALL)

    if not all([title_match, id_match, date_match, url_match, transcript_match]):
        logging.warning(f"[Phase 3] Datei {filepath.name} konnte nicht vollständig geparst werden.")
        return None

    return {
        "title": title_match.group(1).strip(),
        "id": id_match.group(1).strip(),
        "date": date_match.group(1).strip(),
        "url": url_match.group(1).strip(),
        "text": transcript_match.group(1).strip()
    }


def summarize_via_openrouter(video_info: dict) -> str | None:
    """Sendet das Transkript an OpenRouter und gibt die Zusammenfassung zurück."""
    prompt = f"""Du bist ein Experte für professionelle YouTube-Zusammenfassungen im Bereich Finanzen und Börse (Kanal 'Cycle').
Bitte fasse das folgende Transkript so zusammen, dass der Inhalt umfassend und sehr gut verständlich ist.
WICHTIG: Verwende unbedingt Zeitangaben (Timestamps) aus dem Text in deiner Zusammenfassung.
Verzichte auf Bilder oder Markdown-Bilder.

Nutze exakt diesen Kopf für deine Antwort:
# {video_info['title']}
- **Erstellt am:** {video_info['date']}
- **Link:** {video_info['url']}

Zusammenfassung:
...

Hier ist das Transkript:
{video_info['text']}"""

    logging.info(f"[Phase 3] Sende Anfrage an KI (Modell: {AI_MODEL}) für {video_info['id']}...")
    max_retries = 3
    base_delay = 10  # Sekunden

    for attempt in range(max_retries):
        try:
            response = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": AI_MODEL,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=180
            )
            response.raise_for_status()
            data = response.json()

            # Prüfe auf API-interne Fehler im 200-OK-Body (kommt bei Free-Modellen vor)
            if "error" in data:
                logging.warning(f"[Phase 3] OpenRouter API-Fehler im Body: {data['error']}")
                time.sleep(base_delay * (2 ** attempt))
                continue

            choices = data.get("choices")
            if choices and choices[0].get("message", {}).get("content"):
                return choices[0]["message"]["content"].strip()

            logging.error(f"[Phase 3] Leere oder unerwartete Antwort von OpenRouter: {data}")
            return None

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            # Retry bei Rate-Limits (429) sowie temporären Server-Fehlern (5xx)
            if status in (429, 500, 502, 503, 504):
                delay = base_delay * (2 ** attempt)
                logging.warning(
                    f"[Phase 3] HTTP {status} für {video_info['id']}, "
                    f"warte {delay}s vor Retry {attempt + 1}/{max_retries}..."
                )
                time.sleep(delay)
                continue
            logging.error(f"[Phase 3] KI-HTTP-Fehler ({status}): {e}")
            return None
        except requests.exceptions.RequestException as e:
            delay = base_delay * (2 ** attempt)
            logging.warning(
                f"[Phase 3] Netzwerkfehler ({e}), "
                f"warte {delay}s vor Retry {attempt + 1}/{max_retries}..."
            )
            time.sleep(delay)
        except Exception as e:
            logging.error(f"[Phase 3] Unerwarteter KI-Fehler: {e}")
            return None

    logging.error(f"[Phase 3] Maximale Anzahl an Versuchen für {video_info['id']} überschritten.")
    return None


def save_summary_md(video_info: dict, summary_text: str) -> Path | None:
    """Speichert die generierte KI-Zusammenfassung als Markdown-Datei."""
    timestamp_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d_%H%M")
    filename = f"Cycle_Zusammenfassung_{timestamp_str}_{video_info['id']}.md"
    filepath = MD_DIR / filename
    try:
        with filepath.open("w", encoding="utf-8") as f:
            f.write(summary_text)
        return filepath
    except Exception as e:
        logging.error(f"[Phase 3] Speicherfehler bei Zusammenfassung {video_info['id']}: {e}")
        return None


def send_to_telegram(filepath: Path, caption: str = "") -> bool:
    """Sendet die Zusammenfassungs-Datei an den konfigurierten Telegram-Chat."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    try:
        with filepath.open("rb") as document:
            logging.info(f"[Phase 3] Sende {filepath.name} an Telegram...")
            payload = {"chat_id": CHAT_ID}
            if caption:
                payload["caption"] = caption[:1024]  # Telegram-Limit für Captions

            resp = requests.post(
                url,
                data=payload,
                files={"document": document},
                timeout=30  # Verhindert endloses Hängen des Cronjobs
            )
            resp.raise_for_status()
        return True
    except Exception as e:
        logging.error(f"[Phase 3] Telegram-Versandfehler: {e}")
        return False


# ==========================================
# MASTER EXECUTION
# ==========================================

def main() -> None:
    logging.info("=== START MASTER SCRIPT (Cycle) ===")

    # --- PHASE 1: Neue Links finden ---
    known_videos = load_history(HISTORY_FILE_VIDEOS)
    new_videos = fetch_new_videos(known_videos)
    if new_videos:
        save_video_list_md(new_videos)
        for v in new_videos:
            append_to_history(HISTORY_FILE_VIDEOS, v["id"])
            known_videos.add(v["id"])
    else:
        logging.info("[Phase 1] Keine neuen Videos gefunden.")

    # --- PHASE 2: Transkripte der MD-Listen laden ---
    known_transcripts = load_history(HISTORY_FILE_TRANSCRIPTS)
    video_files = sorted(
        f for f in MD_DIR.glob("Cycle_videos_*.md")
        if not f.name.startswith("erledigt_")
    )

    for md_file in video_files:
        videos = parse_video_md_file(md_file)
        if not videos:
            logging.warning(f"[Phase 2] Keine Videos in {md_file.name} erkannt. Markiere als erledigt.")
            mark_file_as_done(md_file)
            continue

        all_processed = True
        for video in videos:
            vid_id = video["id"]
            if vid_id in known_transcripts:
                continue

            text = fetch_transcript(vid_id)
            if text and save_transcript_md(video, text):
                append_to_history(HISTORY_FILE_TRANSCRIPTS, vid_id)
                known_transcripts.add(vid_id)
            else:
                all_processed = False

        # Wenn alle Videos verarbeitet wurden ODER die Datei älter als 48h ist (Schutz vor Endlosschleife)
        if all_processed:
            mark_file_as_done(md_file)
        elif is_file_older_than(md_file, hours=MAX_VIDEO_FILE_AGE_HOURS):
            logging.warning(
                f"[Phase 2] {md_file.name} ist älter als {MAX_VIDEO_FILE_AGE_HOURS}h. "
                "Fehlende Transkripte werden übersprungen und Datei als erledigt markiert."
            )
            mark_file_as_done(md_file)

    # --- PHASE 3: Zusammenfassen & an Telegram senden ---
    known_sent = load_history(HISTORY_FILE_SENT)
    transcript_files = sorted(
        f for f in MD_DIR.glob("Cycle_Transkript_*.md")
        if not f.name.startswith("erledigt_")
    )

    for md_file in transcript_files:
        info = parse_transcript_md(md_file)
        if not info:
            continue

        vid_id = info["id"]
        if vid_id in known_sent:
            mark_file_as_done(md_file)
            continue

        # Bestehende Zusammenfassung suchen, um doppelte KI-Aufrufe zu vermeiden,
        # falls im vorherigen Lauf nur der Telegram-Versand fehlgeschlagen ist
        existing_summaries = sorted(MD_DIR.glob(f"Cycle_Zusammenfassung_*_{vid_id}.md"))
        if existing_summaries:
            summary_path = existing_summaries[-1]
            logging.info(f"[Phase 3] Nutze bereits vorhandene Zusammenfassung: {summary_path.name}")
        else:
            summary = summarize_via_openrouter(info)
            if not summary:
                continue
            summary_path = save_summary_md(info, summary)

        if summary_path and send_to_telegram(summary_path, caption=f"📄 {info['title']}"):
            append_to_history(HISTORY_FILE_SENT, vid_id)
            known_sent.add(vid_id)
            mark_file_as_done(md_file)

    logging.info("=== MASTER SCRIPT BEENDET (Cycle) ===")


if __name__ == "__main__":
    main()
