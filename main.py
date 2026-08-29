"""
Spotify Audio Extractor API
---------------------------
Spotify track linkidan metadata (nom, muallif, cover) oladi va
YouTube'dan mos audio manbani yt-dlp orqali topib beradi.

Arxitektura:
  1. SpotifyMetadataService  -> Spotify embed sahifasidan metadata scrape qiladi
  2. YouTubeAudioService     -> yt-dlp orqali audio stream URL topadi
  3. FastAPI endpoint        -> ikkalasini birlashtiradi, xatolarni tartibli qaytaradi
"""

import json
import logging
import os
import re
import shutil
from typing import Optional

import requests
import yt_dlp
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

# --------------------------------------------------------------------------
# Logging sozlamalari - Render Logs'da tartibli ko'rinishi uchun
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("spotify-audio-api")

# --------------------------------------------------------------------------
# Konfiguratsiya
# --------------------------------------------------------------------------
RAPIDAPI_PROXY_SECRET = os.getenv("RAPIDAPI_PROXY_SECRET", "")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _resolve_cookies_file() -> Optional[str]:
    """
    Cookie faylini topadi va yt-dlp yoza oladigan (/tmp) joyga nusxalaydi.
    Render Secret Files joyi (/etc/secrets/) READ-ONLY bo'lgani uchun
    yt-dlp'ning o'zi cookie'ni yangilamoqchi bo'lsa xato beradi - shuning
    uchun har doim yoziladigan nusxa bilan ishlaymiz.
    """
    candidates = [
        os.getenv("COOKIES_PATH", ""),
        "/etc/secrets/cookies.txt",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt"),
    ]
    source = next((p for p in candidates if p and os.path.isfile(p)), None)

    if not source:
        logger.warning("cookies fayli topilmadi - cookie'siz davom etiladi")
        return None

    try:
        writable_path = "/tmp/cookies_runtime.txt"
        shutil.copyfile(source, writable_path)
        logger.info("cookies fayli tayyor: %s -> %s", source, writable_path)
        return writable_path
    except OSError as e:
        logger.error("cookies faylini /tmp ga nusxalab bo'lmadi: %s", e)
        return None


COOKIES_FILE = _resolve_cookies_file()


# --------------------------------------------------------------------------
# Domain modellar
# --------------------------------------------------------------------------
class TrackMetadata(BaseModel):
    title: str
    artist: str
    cover_url: Optional[str] = None


class AudioSource(BaseModel):
    download_url: str
    duration: str
    bitrate: int


class ExtractionError(Exception):
    """Aniq bosqichda nima xato bo'lganini bildirish uchun maxsus exception."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        self.message = message
        super().__init__(f"[{stage}] {message}")


# --------------------------------------------------------------------------
# 1. Spotify metadata xizmati
# --------------------------------------------------------------------------
class SpotifyMetadataService:
    TRACK_ID_RE = re.compile(r"track/([a-zA-Z0-9]+)")

    @classmethod
    def extract_track_id(cls, spotify_url: str) -> Optional[str]:
        match = cls.TRACK_ID_RE.search(spotify_url)
        return match.group(1) if match else None

    @classmethod
    def fetch(cls, spotify_url: str) -> TrackMetadata:
        track_id = cls.extract_track_id(spotify_url)
        if not track_id:
            raise ExtractionError("spotify", "URL ichidan track ID topilmadi")

        embed_url = f"https://open.spotify.com/embed/track/{track_id}"

        try:
            response = requests.get(
                embed_url, headers={"User-Agent": USER_AGENT}, timeout=10
            )
        except requests.RequestException as e:
            raise ExtractionError("spotify", f"So'rov yuborishda xato: {e}")

        if response.status_code != 200:
            raise ExtractionError(
                "spotify", f"Spotify embed HTTP {response.status_code} qaytardi"
            )

        soup = BeautifulSoup(response.text, "html.parser")

        # Asosiy usul: __NEXT_DATA__ script tegidagi JSON
        metadata = cls._parse_next_data(soup)
        if metadata:
            return metadata

        # Zaxira usul: og: meta teglar
        metadata = cls._parse_og_tags(soup)
        if metadata:
            return metadata

        raise ExtractionError(
            "spotify", "Sahifadan metadata ajratib bo'lmadi (struktura o'zgargan bo'lishi mumkin)"
        )

    @staticmethod
    def _parse_next_data(soup: BeautifulSoup) -> Optional[TrackMetadata]:
        script_tag = soup.find("script", id="__NEXT_DATA__")
        if not script_tag or not script_tag.string:
            return None

        try:
            data = json.loads(script_tag.string)
            entity = data["props"]["pageProps"]["state"]["data"]["entity"]
            title = entity.get("title")
            if not title:
                return None

            artists = entity.get("artists", [])
            artist_name = ", ".join(a.get("name", "") for a in artists) or "Unknown Artist"
            cover_url = entity.get("visualIdentity", {}).get("image", [{}])[0].get("url")

            return TrackMetadata(title=title, artist=artist_name, cover_url=cover_url)
        except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
            logger.debug("NEXT_DATA parse qilinmadi: %s", e)
            return None

    @staticmethod
    def _parse_og_tags(soup: BeautifulSoup) -> Optional[TrackMetadata]:
        title_tag = soup.find("meta", property="og:title")
        if not title_tag or not title_tag.get("content"):
            return None

        image_tag = soup.find("meta", property="og:image")
        desc_tag = soup.find("meta", property="og:description")

        artist = "Unknown Artist"
        if desc_tag and desc_tag.get("content") and " · " in desc_tag["content"]:
            parts = desc_tag["content"].split(" · ")
            if len(parts) > 1:
                artist = parts[1]

        return TrackMetadata(
            title=title_tag["content"],
            artist=artist,
            cover_url=image_tag["content"] if image_tag else None,
        )


# --------------------------------------------------------------------------
# 2. YouTube audio xizmati (yt-dlp asosida)
# --------------------------------------------------------------------------
class YouTubeAudioService:
    # Turli video/client kombinatsiyalarida format topilmasligi mumkin,
    # shuning uchun bir nechta strategiyani ketma-ket sinaymiz.
    _CLIENT_STRATEGIES = [
        ["android", "web"],
        ["ios"],
        ["tv_embedded", "web"],
        ["web"],
    ]

    @staticmethod
    def _build_ydl_opts(player_clients: list, format_selector: str) -> dict:
        opts = {
            "format": format_selector,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch1",
            "skip_download": True,
            "socket_timeout": 15,
            "cachedir": False,
            "extractor_args": {
                "youtube": {
                    "player_client": player_clients,
                }
            },
        }
        if COOKIES_FILE:
            opts["cookiefile"] = COOKIES_FILE
        return opts

    @classmethod
    def find_audio(cls, search_query: str) -> AudioSource:
        last_error: Optional[str] = None

        for player_clients in cls._CLIENT_STRATEGIES:
            # Har bir client uchun avval audio-only, keyin har qanday format bilan urinamiz
            for format_selector in ("bestaudio/best", "best"):
                try:
                    opts = cls._build_ydl_opts(player_clients, format_selector)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(search_query, download=False)
                except yt_dlp.utils.DownloadError as e:
                    last_error = f"client={player_clients} format={format_selector}: {str(e)[:200]}"
                    logger.debug("Strategiya muvaffaqiyatsiz: %s", last_error)
                    continue
                except Exception as e:
                    last_error = f"client={player_clients}: kutilmagan xato {type(e).__name__}: {str(e)[:200]}"
                    continue

                if not info:
                    last_error = f"client={player_clients}: bo'sh natija"
                    continue

                if "entries" in info:
                    entries = [e for e in info["entries"] if e]
                    if not entries:
                        last_error = f"client={player_clients}: qidiruv natijasi bo'sh"
                        continue
                    info = entries[0]

                audio_format = cls._pick_best_audio_format(info)
                if not audio_format or not audio_format.get("url"):
                    last_error = f"client={player_clients}: mos audio format topilmadi"
                    continue

                duration_sec = int(info.get("duration") or 0)
                minutes, seconds = divmod(duration_sec, 60)

                logger.info(
                    "Audio topildi (client=%s, format=%s)", player_clients, format_selector
                )
                return AudioSource(
                    download_url=audio_format["url"],
                    duration=f"{minutes}:{seconds:02d}",
                    bitrate=round(audio_format.get("abr") or 128),
                )

        raise ExtractionError(
            "youtube", f"Barcha strategiyalar muvaffaqiyatsiz. Oxirgi xato: {last_error}"
        )

    @staticmethod
    def _pick_best_audio_format(info: dict) -> Optional[dict]:
        formats = info.get("formats", [])

        audio_only = [
            f for f in formats
            if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
        ]
        if audio_only:
            return max(audio_only, key=lambda f: f.get("abr") or 0)

        # Audio-only format bo'lmasa, umumiy eng yaxshi format bilan qanoatlanamiz
        if info.get("url"):
            return info

        return None


# --------------------------------------------------------------------------
# FastAPI ilova
# --------------------------------------------------------------------------
app = FastAPI(
    title="Spotify Audio Extractor API",
    description="Spotify track linkidan metadata va YouTube orqali audio manba topadi.",
    version="2.0.0",
)


@app.get("/health")
async def health_check():
    return {"status": "ok", "cookies_loaded": COOKIES_FILE is not None}


@app.get("/api/v1/convert")
async def convert_spotify_track(
    spotify_url: str = Query(..., description="Spotify track URL"),
    x_rapidapi_proxy_secret: Optional[str] = Header(None, alias="X-RapidAPI-Proxy-Secret"),
):
    if RAPIDAPI_PROXY_SECRET and x_rapidapi_proxy_secret != RAPIDAPI_PROXY_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid Proxy Secret")

    try:
        metadata = SpotifyMetadataService.fetch(spotify_url)
    except ExtractionError as e:
        logger.warning("Spotify metadata xatosi: %s", e.message)
        raise HTTPException(status_code=400, detail=f"Spotify metadata xatosi: {e.message}")

    search_query = f"{metadata.artist} {metadata.title}"

    try:
        audio = YouTubeAudioService.find_audio(search_query)
    except ExtractionError as e:
        logger.warning("YouTube audio xatosi (%s): %s", search_query, e.message)
        raise HTTPException(status_code=404, detail=f"Audio topilmadi: {e.message}")

    return {
        "status": "success",
        "data": {
            "title": metadata.title,
            "artist": metadata.artist,
            "cover_url": metadata.cover_url,
            "duration": audio.duration,
            "download_url": audio.download_url,
            "bitrate": audio.bitrate,
        },
    }
