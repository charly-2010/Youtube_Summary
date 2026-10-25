# Dateiname: YouTube_Master_Summarizer_Fokusinvestor.py
# Voraussetzungen: pip install yt-dlp youtube-transcript-api requests python-dotenv

import os
import glob
import re
import logging
import requests
import yt_dlp
from datetime import datetime, timedelta, timezone
from youtube_transcript_api import YouTubeTranscriptApi
from dotenv import load_dotenv

# ==========================================
# 0. Initialisierung & Konfiguration
# ==========================================

# Logging konfigurieren
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Umgebungsvariablen laden (Cronjob-sicher)
script_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(script_dir, '.env')
load_dotenv(env_path)

openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")

if not all([openrouter_api_key, bot_token, chat_id]):
    raise ValueError(f"Fehlende API-Keys! Konnte sie nicht aus {env_path} laden. Bitte prüfe die .env-Datei.")

# Allgemeine Einstellungen (Angepasst für Fokusinvestor)
TARGET_URL = "https://www.youtube.com/@FSCtv/videos"
MD_DIR = "/home/freddy/Dokumente/Obsidian_YT_MD_3M_cycleinvestor"
AI_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

# Historien-Dateien (Namensschema auf Fokusinvestor)
HISTORY_FILE_VIDEOS = os.path.join(MD_DIR, "cycleinvestor_verarbeitete_videos.txt")
HISTORY_FILE_TRANSCRIPTS = os.path.join(MD_DIR, "cycleinvestor_geladene_transkripte.txt")
HISTORY_FILE_SENT = os.path.join(MD_DIR, "cycleinvestor_versendete_zusammenfassungen.txt")

# Stelle sicher, dass das Verzeichnis existiert
os.makedirs(MD_DIR, exist_ok=True)


# ==========================================
# Hilfsfunktionen (Allgemein)
# ==========================================

def load_history(filepath):
    """Lädt die Liste bereits verarbeiteter IDs."""
    if not os.path.exists(filepath):
        return set()
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        return set(line.strip() for line in f if line.strip())

def append_to_history(filepath, item_id):
    """Speichert eine ID in die jeweilige Historie."""
    with open(filepath, 'a', encoding='utf-8-sig') as f:
        f.write(f"{item_id}\n")


# ==========================================
# PHASE 1: Links ermitteln
# ==========================================

def fetch_new_videos(known_ids):
    # Erhöht auf 200, um sicherzugehen, dass ausreichend Videos der letzten Monate abgedeckt werden
    ydl_opts = {'extract_flat': False, 'quiet': True, 'no_warnings': True, 'playlistend': 30}
    recent_videos = []
    
    # Stichtag festlegen: Letzte 3 Monate (ca. 90 Tage)
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=90)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logging.info(f"[Phase 1] Analysiere Kanal: {TARGET_URL}...")
            info = ydl.extract_info(TARGET_URL, download=False)

            if 'entries' not in info:
                return []

            for entry in info['entries']:
                if not entry: continue
                video_id = entry.get('id')
                
                if not video_id or video_id in known_ids:
                    continue

                video_date = None
                if entry.get('release_timestamp'):
                    video_date = datetime.fromtimestamp(entry['release_timestamp'], tz=timezone.utc)
                elif entry.get('timestamp'):
                    video_date = datetime.fromtimestamp(entry['timestamp'], tz=timezone.utc)
                elif entry.get('upload_date'):
                    try:
                        video_date = datetime.strptime(entry['upload_date'], '%Y%m%d').replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
                
                if not video_date: continue

                # Filterung nach Zeitraum: Videos, die in den letzten 90 Tagen hochgeladen wurden
                if video_date >= cutoff_date:
                    duration_str = str(timedelta(seconds=entry.get('duration'))) if entry.get('duration') else "Unbekannt"
                    recent_videos.append({
                        'title': entry.get('title', 'Ohne Titel'),
                        'url': entry.get('webpage_url', f"https://www.youtube.com/watch?v={video_id}"),
                        'id': video_id,
                        'date': video_date,
                        'duration': duration_str
                    })
        return recent_videos
    except Exception as e:
        logging.error(f"[Phase 1] Fehler beim Abrufen der Videos: {e}")
        return []

def save_video_list_md(videos):
    if not videos: return
    timestamp_str = datetime.now().strftime("%Y-%m-%d_%H%M")
    filepath = os.path.join(MD_DIR, f"Fokusinvestor_videos_{timestamp_str}.md")
    try:
        with open(filepath, "w", encoding="utf-8-sig") as f:
            f.write("# Neue Videos: Fokusinvestor (Letzte 3 Monate)\n\n")
            for v in videos:
                date_str = v['date'].strftime('%d.%m.%Y um %H:%M')
                f.write(f"### [{v['title']}]({v['url']})\n")
                f.write(f"- **Video-ID:** {v['id']}\n")
                f.write(f"- **Veröffentlicht am:** {date_str} Uhr\n")
                f.write(f"- **Dauer:** {v['duration']}\n\n")
        logging.info(f"[Phase 1] {len(videos)} neue(s) Video(s) gespeichert.")
    except Exception as e:
        logging.error(f"[Phase 1] Fehler beim Schreiben: {e}")


# ==========================================
# PHASE 2: Transkripte laden
# ==========================================

def parse_video_md_file(filepath):
    videos = []
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        content = f.read()
    pattern = r"### \[(.*?)\]\((.*?)\)\n- \*\*Video-ID:\*\* (.*?)\n- \*\*Veröffentlicht am:\*\* (.*?)\n"
    for match in re.finditer(pattern, content):
        videos.append({
            "title": match.group(1).strip(), "url": match.group(2).strip(),
            "id": match.group(3).strip(), "date": match.group(4).strip()
        })
    return videos

def fetch_transcript(video_id):
    try:
        logging.info(f"[Phase 2] Lade Transkript für {video_id}...")
        # Erst Englisch versuchen, dann Deutsch als Fallback
        transcript = YouTubeTranscriptApi().fetch(video_id, languages=['en', 'de'])
        lines = [f"[{int(e.start//60):02d}:{int(e.start%60):02d}] {e.text}" for e in transcript]
        return "\n".join(lines)
    except Exception as e:
        logging.warning(f"[Phase 2] Transkript {video_id} nicht verfügbar: {e}")
        return None

def save_transcript_md(video_info, transcript_text):
    timestamp_str = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"Fokusinvestor_Transkript_{timestamp_str}_{video_info['id']}.md"
    filepath = os.path.join(MD_DIR, filename)
    try:
        with open(filepath, "w", encoding="utf-8-sig") as f:
            f.write(f"# Roh-Transkript: {video_info['title']}\n\n")
            f.write(f"- **Video-ID:** {video_info['id']}\n")
            f.write(f"- **Veröffentlicht am:** {video_info['date']}\n")
            f.write(f"- **Link zum Video:** {video_info['url']}\n\n---\n\n")
            f.write(f"## Transkript-Text\n\n{transcript_text}")
        return True
    except Exception as e:
        logging.error(f"[Phase 2] Speicherfehler: {e}")
        return False


# ==========================================
# PHASE 3: Zusammenfassen & Senden
# ==========================================

def parse_transcript_md(filepath):
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        content = f.read()
    title_match = re.search(r"# Roh-Transkript: (.*)", content)
    id_match = re.search(r"- \*\*Video-ID:\*\* (.*)", content)
    date_match = re.search(r"- \*\*Veröffentlicht am:\*\* (.*)", content)
    url_match = re.search(r"- \*\*Link zum Video:\*\* (.*)", content)
    transcript_match = re.search(r"## Transkript-Text\n\n(.*)", content, re.DOTALL)

    if not all([title_match, id_match, date_match, url_match, transcript_match]): return None
    return {
        "title": title_match.group(1).strip(), "id": id_match.group(1).strip(),
        "date": date_match.group(1).strip(), "url": url_match.group(1).strip(),
        "text": transcript_match.group(1).strip()
    }

def summarize_via_openrouter(video_info):
    prompt = f"""Du bist ein Experte für professionelle YouTube-Zusammenfassungen im Bereich Finanzen und Börse (Kanal 'Cycle Investor').
Bitte fasse das folgende Transkript so zusammen, dass der Inhalt umfassend und sehr gut verständlich ist.
WICHTIG: Verwende unbedingt Zeitangaben (Timestamps) aus dem Text in deiner Zusammenfassung.
Verzichte auf Bilder oder Markdown-Bilder. 
ANTWORTE AUF DEUTSCH.

Nutze exakt diesen Kopf für deine Antwort:
# {video_info['title']}
- **Erstellt am:** {video_info['date']}
- **Link:** {video_info['url']}

Zusammenfassung:
...

Hier ist das Transkript:
{video_info['text']}"""

    logging.info(f"[Phase 3] Sende Anfrage an KI (Modell: {AI_MODEL}) für {video_info['id']}...")
    try:
        response = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {openrouter_api_key}", "Content-Type": "application/json"},
            json={"model": AI_MODEL, "messages": [{"role": "user", "content": prompt}]},
            timeout=180
        )
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except Exception as e:
        logging.error(f"[Phase 3] KI-Fehler: {e}")
        return None

def save_summary_md(video_info, summary_text):
    filename = f"Fokusinvestor_Zusammenfassung_{datetime.now().strftime('%Y-%m-%d_%H%M')}_{video_info['id']}.md"
    filepath = os.path.join(MD_DIR, filename)
    try:
        with open(filepath, "w", encoding="utf-8-sig") as f:
            f.write(summary_text)
        return filepath
    except Exception as e:
        logging.error(f"[Phase 3] Speicherfehler: {e}")
        return None

def send_to_telegram(filepath):
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    try:
        with open(filepath, 'rb') as document:
            logging.info(f"[Phase 3] Sende an Telegram...")
            resp = requests.post(url, data={'chat_id': chat_id}, files={'document': document})
            resp.raise_for_status()
        return True
    except Exception as e:
        logging.error(f"[Phase 3] Telegram-Fehler: {e}")
        return False


# ==========================================
# MASTER EXECUTION
# ==========================================

def main():
    logging.info("=== START MASTER SCRIPT (Fokusinvestor) ===")

    # --- PHASE 1: Neue Links finden ---
    known_videos = load_history(HISTORY_FILE_VIDEOS)
    new_videos = fetch_new_videos(known_videos)
    if new_videos:
        save_video_list_md(new_videos)
        for s in new_videos: append_to_history(HISTORY_FILE_VIDEOS, s['id'])
    else:
        logging.info("[Phase 1] Keine neuen Videos im Zeitraum (letzte 3 Monate) gefunden.")

    # --- PHASE 2: Transkripte der MD-Listen laden ---
    known_transcripts = load_history(HISTORY_FILE_TRANSCRIPTS)
    video_files = [f for f in glob.glob(os.path.join(MD_DIR, "Fokusinvestor_videos_*.md")) if not os.path.basename(f).startswith("erledigt_")]
    
    for md_file in video_files:
        videos = parse_video_md_file(md_file)
        all_processed = True
        for video in videos:
            if video['id'] in known_transcripts: continue
            text = fetch_transcript(video['id'])
            if text and save_transcript_md(video, text):
                append_to_history(HISTORY_FILE_TRANSCRIPTS, video['id'])
                known_transcripts.add(video['id'])
            else:
                all_processed = False
        
        if all_processed and videos:
            os.rename(md_file, os.path.join(MD_DIR, "erledigt_" + os.path.basename(md_file)))

    # --- PHASE 3: Zusammenfassen & an Telegram senden ---
    known_sent = load_history(HISTORY_FILE_SENT)
    transcript_files = [f for f in glob.glob(os.path.join(MD_DIR, "Fokusinvestor_Transkript_*.md")) if not os.path.basename(f).startswith("erledigt_")]

    for md_file in transcript_files:
        info = parse_transcript_md(md_file)
        if not info: continue
        if info['id'] in known_sent:
            os.rename(md_file, os.path.join(MD_DIR, "erledigt_" + os.path.basename(md_file)))
            continue

        summary = summarize_via_openrouter(info)
        if summary:
            summary_path = save_summary_md(info, summary)
            if summary_path and send_to_telegram(summary_path):
                append_to_history(HISTORY_FILE_SENT, info['id'])
                os.rename(md_file, os.path.join(MD_DIR, "erledigt_" + os.path.basename(md_file)))

    logging.info("=== MASTER SCRIPT BEENDET ===")

if __name__ == "__main__":
    main()