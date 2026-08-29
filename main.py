import asyncio
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Query
import requests
from yt_dlp import YoutubeDL

app = FastAPI(
    title="Spotify Professional Audio & Metadata Extractor API",
    description="Spotify havolasidan to'liq meta-ma'lumotlar va direct audio stream manzilini ajratib beruvchi API",
    version="1.0.0",
)

# RapidAPI Studio'dan olingan secret kalitingiz
RAPIDAPI_SECRET = "d90fe7d0-a377-11f1-a0ae-1d5fd5492d49"


def format_duration(seconds: Optional[int]) -> str:
    """Sekundlarni MM:SS formatiga o'tkazish (masalan: 215 -> 03:35)"""
    if not seconds:
        return "00:00"
    minutes = seconds // 60
    rem_seconds = seconds % 60
    return f"{minutes:02d}:{rem_seconds:02d}"


def get_spotify_metadata_oembed(spotify_url: str) -> dict:
    """Spotify'ning rasmiy oEmbed interfeysidan trek detallarini olish"""
    try:
        oembed_url = f"https://open.spotify.com/oembed?url={spotify_url}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(oembed_url, headers=headers, timeout=10)

        if response.status_code != 200:
            raise ValueError(
                f"Spotify havolasi topilmadi yoki yaroqsiz (Status: {response.status_code})"
            )

        data = response.json()

        title = data.get("title", "Noma'lum trek")
        artist = data.get("author_name", "Noma'lum ijrochi")
        cover_url = data.get("thumbnail_url", "")

        search_query = f"{artist} - {title}".strip() if artist else title

        return {
            "title": title,
            "artist": artist,
            "cover": cover_url,
            "search_query": search_query,
        }
    except Exception as e:
        raise ValueError(
            f"Spotify metadatasini ajratishda xatolik: {str(e)}"
        )


def extract_audio_details(search_query: str) -> dict:
    """yt-dlp orqali audio stream va texnik ko'rsatkichlarni ajratib olish"""
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "default_search": "ytsearch1:",
        "noplaylist": True,
        "nocheckcertificate": True,
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        },
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)

            if "entries" in info and len(info["entries"]) > 0:
                audio_data = info["entries"][0]
            else:
                audio_data = info

            duration_sec = audio_data.get("duration", 0)

            return {
                "matched_title": audio_data.get("title"),
                "source_url": audio_data.get("webpage_url"),
                "download_url": audio_data.get("url"),
                "duration_seconds": duration_sec,
                "duration_formatted": format_duration(duration_sec),
                "audio_bitrate": audio_data.get("abr"),
                "audio_ext": audio_data.get("ext", "mp3"),
                "views": audio_data.get("view_count"),
            }
    except Exception as err:
        raise ValueError(
            f"Audio manbasini ajratib bo'lmadi: {str(err)}"
        )


@app.get("/api/v1/convert")
async def convert_spotify_track(
    spotify_url: str = Query(
        ...,
        description="Spotify qo'shiq havolasi (masalan: https://open.spotify.com/track/...)",
    ),
    x_rapidapi_proxy_secret: Optional[str] = Header(
        None, alias="X-RapidAPI-Proxy-Secret"
    ),
):
    # RapidAPI havfsizlik tekshiruvi (Lokal testing paytida header yuborilmasa o'tishi uchun)
    if (
        x_rapidapi_proxy_secret
        and x_rapidapi_proxy_secret != RAPIDAPI_SECRET
    ):
        raise HTTPException(
            status_code=403,
            detail="Ruxsat berilmadi: Xavfsizlik kaliti noto'g'ri.",
        )

    try:
        loop = asyncio.get_event_loop()

        # 1-bosqich: Metadata olish
        spotify_data = await loop.run_in_executor(
            None, get_spotify_metadata_oembed, spotify_url
        )

        # 2-bosqich: Audio va qo'shimcha detallarni olish
        audio_data = await loop.run_in_executor(
            None, extract_audio_details, spotify_data["search_query"]
        )

        # Telegram botlar va boshqa frontend loyihalar uchun professional JSON tuzilmasi
        return {
            "status": "success",
            "metadata": {
                "title": spotify_data["title"],
                "artist": spotify_data["artist"],
                "cover_url": spotify_data["cover"],
            },
            "audio_details": {
                "duration_seconds": audio_data["duration_seconds"],
                "duration_formatted": audio_data["duration_formatted"],
                "format": audio_data["audio_ext"],
                "bitrate_kbps": audio_data["audio_bitrate"],
                "download_url": audio_data["download_url"],
            },
            "source_info": {
                "platform": "YouTube Music / YouTube",
                "matched_title": audio_data["matched_title"],
                "original_url": audio_data["source_url"],
                "views": audio_data["views"],
            },
        }

    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as err:
        raise HTTPException(
            status_code=500, detail=f"Server ichki xatoligi: {str(err)}"
        )