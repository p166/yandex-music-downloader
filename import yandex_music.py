import yandex_music
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TALB, TDRC, TCON, TRCK, TPOS, APIC,
)


def load_dotenv(dotenv_path: str = ".env") -> None:
    if not os.path.exists(dotenv_path):
        return

    with open(dotenv_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv()

# === ПОЛЬЗОВАТЕЛЬСКИЕ НАСТРОЙКИ ===
YOUR_SESSION_ID = os.getenv("YANDEX_MUSIC_SESSION_ID", "")

DOWNLOAD_DIR = "./yandex_music_downloads"  # папка, куда будут лежать mp3
HQ = True  # True — высокое качество, False — стандартное
MAX_WORKERS = 4  # число параллельных загрузок

# https://github.com/MarshalX/yandex-music-api/discussions/513#discussioncomment-2729781

def safe_filename(value: str) -> str:
    # Replace path separators and filesystem-reserved characters.
    cleaned = re.sub(r"[\\/:*?\"<>|]", "_", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned or "Unknown"


def load_downloaded_ids(index_path: str) -> set[str]:
    if not os.path.exists(index_path):
        return set()

    with open(index_path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def save_downloaded_id(index_path: str, track_id: str) -> None:
    with open(index_path, "a", encoding="utf-8") as f:
        f.write(f"{track_id}\n")


def ensure_download_dir(download_dir: str) -> None:
    if os.path.exists(download_dir) and not os.path.isdir(download_dir):
        raise SystemExit(f"Ошибка: путь существует и это не каталог: {download_dir}")

    if not os.path.isdir(download_dir):
        os.makedirs(download_dir, exist_ok=True)
        print(f"Каталог создан: {download_dir}")


def get_artists(track) -> str:
    artists = ", ".join(
        artist.name for artist in (track.artists or []) if getattr(artist, "name", None)
    )
    return artists or "Unknown"


def write_id3_tags(fpath: str, track) -> None:
    try:
        tags = ID3(fpath)
    except ID3NoHeaderError:
        tags = ID3()

    title = track.title or ""
    if getattr(track, "version", None):
        title += f" ({track.version})"

    tags["TIT2"] = TIT2(encoding=3, text=title)
    tags["TPE1"] = TPE1(encoding=3, text=get_artists(track))

    album = (track.albums or [None])[0]
    if album:
        if album.title:
            tags["TALB"] = TALB(encoding=3, text=album.title)
        if getattr(album, "year", None):
            tags["TDRC"] = TDRC(encoding=3, text=str(album.year))
        if getattr(album, "genre", None):
            tags["TCON"] = TCON(encoding=3, text=album.genre)
        pos = getattr(album, "track_position", None)
        if pos:
            trck = f"{pos.index}/{album.track_count}" if album.track_count else str(pos.index)
            tags["TRCK"] = TRCK(encoding=3, text=trck)
            tags["TPOS"] = TPOS(encoding=3, text=str(pos.volume))

    cover_uri = getattr(track, "cover_uri", None)
    if cover_uri:
        url = cover_uri.replace("%%", "400x400")
        if not url.startswith("http"):
            url = "https://" + url
        try:
            resp = requests.get(url, timeout=10)
            if resp.ok:
                tags["APIC"] = APIC(
                    encoding=3, mime="image/jpeg", type=3, desc="Cover", data=resp.content
                )
        except Exception:
            pass

    tags.save(fpath)


# === МАССОВОЕ СКАЧИВАНИЕ «МНЕ НРАВИТСЯ» ===
def download_liked(session_id: str, download_dir: str, hq: bool = False):
    client = yandex_music.Client(session_id).init()
    likes = client.users_likes_tracks()
    total_tracks = len(likes.tracks)

    print(f"Найдено треков в 'Мне нравится': {total_tracks}")
    print(f"Параллельных загрузок: {MAX_WORKERS}")

    os.makedirs(download_dir, exist_ok=True)

    downloaded_index_path = os.path.join(download_dir, ".downloaded_ids.txt")
    downloaded_ids = load_downloaded_ids(downloaded_index_path)
    ids_lock = threading.Lock()

    def process_track(i: int, track_short) -> None:
        try:
            track = track_short.fetch_track()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[{i}/{total_tracks}] Ошибка получения трека: {e}")
            return

        if track is None:
            print(f"[{i}/{total_tracks}] Невозможно загрузить трек")
            return

        track_id = str(getattr(track, "id", ""))
        if track_id:
            with ids_lock:
                if track_id in downloaded_ids:
                    print(f"[{i}/{total_tracks}] Уже скачан по ID: {track_id}")
                    return

        title = track.title or "Unknown title"
        artists = get_artists(track)
        fname = safe_filename(f"{artists} - {title}") + ".mp3"
        fpath = os.path.join(download_dir, fname)

        if os.path.exists(fpath):
            print(f"[{i}/{total_tracks}] Уже есть: {fpath}")
            if track_id:
                with ids_lock:
                    if track_id not in downloaded_ids:
                        save_downloaded_id(downloaded_index_path, track_id)
                        downloaded_ids.add(track_id)
            return

        print(f"[{i}/{total_tracks}] Скачиваю: {artists} - {title}")
        try:
            track.download(fpath, bitrate_in_kbps=320 if hq else 192)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[{i}/{total_tracks}] Ошибка скачивания: {e}")
            return

        try:
            write_id3_tags(fpath, track)
        except Exception as e:
            print(f"[{i}/{total_tracks}] Ошибка записи тегов: {e}")

        if track_id:
            with ids_lock:
                if track_id not in downloaded_ids:
                    save_downloaded_id(downloaded_index_path, track_id)
                    downloaded_ids.add(track_id)

    futures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for i, track_short in enumerate(likes.tracks, 1):
            futures.append(executor.submit(process_track, i, track_short))

        try:
            for future in as_completed(futures):
                future.result()
        except KeyboardInterrupt:
            print("\nОстановка по Ctrl+C. Завершаю активные задачи...")
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise


if __name__ == "__main__":
    ensure_download_dir(DOWNLOAD_DIR)

    if not YOUR_SESSION_ID:
        print("Укажи переменную окружения YANDEX_MUSIC_SESSION_ID")
    else:
        try:
            download_liked(YOUR_SESSION_ID, DOWNLOAD_DIR, HQ)
        except KeyboardInterrupt:
            print("Работа остановлена пользователем.")
        