import json
import os
import re
import asyncio
from typing import Optional
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Query
import requests
import yt_dlp

app = FastAPI(
    title="Spotify Audio Extractor API",
    description="Professional tool to get metadata and audio from Spotify links.",
    version="1.7.0"
)

RAPIDAPI_PROXY_SECRET = os.getenv("RAPIDAPI_PROXY_SECRET", "d90fe7d0-a377-11f1-a0ae-1d5fd5492d49")

# Fallback instance ro'yxati - agar dinamik ro'yxat olinmasa ishlatiladi
FALLBACK_PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://api.piped.privacydev.net",
    "https://pipedapi.drgns.space",
    "https://piped-api.lunar.icu",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.reallyaweso.me",
]


def get_active_piped_instances():
    """
    piped-instances.kavin.rocks dan jonli (yaxshi holatdagi) instance
    ro'yxatini dinamik oladi. Muvaffaqiyatsiz bo'lsa fallback ro'yxatga tushadi.
    """
    try:
        res = requests.get("https://piped-instances.kavin.rocks/", timeout=6)
        if res.status_code == 200:
            data = res.json()
            instances = [
                item["api_url"] for item in data
                if item.get("api_url") and item.get("cdn", True) is not False
            ]
            if instances:
                # eng ko'p 8 tasini sinaymiz, hammasini emas (vaqt tejash uchun)
                return instances[:8]
    except Exception as e:
        print(f"[WARN] Piped instance ro'yxatini olishda xato: {e}")

    return FALLBACK_PIPED_INSTANCES


def extract_track_id(url: str) -> Optional[str]:
    match = re.search(r"track/([a-zA-Z0-9]+)", url)
    return match.group(1) if match else None


def get_spotify_metadata(spotify_url: str):
    track_id = extract_track_id(spotify_url)
    if not track_id:
        return None

    embed_url = f"https://open.spotify.com/embed/track/{track_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }

    try:
        response = requests.get(embed_url, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"[SPOTIFY] embed status {response.status_code}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        script_tag = soup.find("script", id="__NEXT_DATA__")

        if script_tag and script_tag.string:
            data = json.loads(script_tag.string)
            try:
                entity = data['props']['pageProps']['state']['data']['entity']
                title = entity.get("title")
                artists = entity.get("artists", [])
                artist_name = ", ".join([a.get("name") for a in artists]) if artists else "Unknown Artist"
                cover_url = entity.get("visualIdentity", {}).get("image", [{}])[0].get("url")

                if title:
                    return {"title": title, "artist": artist_name, "cover_url": cover_url}
            except KeyError as e:
                print(f"[SPOTIFY] KeyError NEXT_DATA ichida: {e}")

        # Fallback: og: meta teglar orqali
        title_tag = soup.find("meta", property="og:title")
        image_tag = soup.find("meta", property="og:image")
        desc_tag = soup.find("meta", property="og:description")

        if title_tag:
            return {
                "title": title_tag["content"],
                "artist": desc_tag["content"].split(" · ")[1] if desc_tag and " · " in desc_tag["content"] else "Unknown Artist",
                "cover_url": image_tag["content"] if image_tag else ""
            }
    except Exception as e:
        print(f"[SPOTIFY] EXCEPTION: {type(e).__name__} - {e}")
        return None

    return None


def get_youtube_audio_ytdlp(search_query: str):
    """
    yt-dlp orqali YouTube'dan bevosita audio manba topadi.
    Piped'dan farqli o'laroq, vositachi serverga bog'liq emas.
    """
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "skip_download": True,
        "extract_flat": False,
        "socket_timeout": 10,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)

            if "entries" in info:
                if not info["entries"]:
                    return {"_debug_errors": ["yt-dlp: qidiruv natijasi bo'sh"]}
                info = info["entries"][0]

            formats = info.get("formats", [])
            audio_formats = [
                f for f in formats
                if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
            ]

            if not audio_formats:
                # audio-only topilmasa, eng yaxshi umumiy formatni olamiz
                if info.get("url"):
                    audio_formats = [info]
                else:
                    return {"_debug_errors": ["yt-dlp: audio format topilmadi"]}

            best = max(audio_formats, key=lambda x: x.get("abr") or 0)
            duration_sec = info.get("duration", 0) or 0
            mins, secs = divmod(int(duration_sec), 60)

            return {
                "download_url": best.get("url"),
                "duration": f"{mins}:{secs:02d}",
                "bitrate": round(best.get("abr")) if best.get("abr") else 128,
                "source_instance": "yt-dlp-direct",
            }

    except yt_dlp.utils.DownloadError as e:
        return {"_debug_errors": [f"yt-dlp DownloadError: {str(e)[:300]}"]}
    except Exception as e:
        return {"_debug_errors": [f"yt-dlp EXCEPTION {type(e).__name__}: {str(e)[:300]}"]}


def get_youtube_audio_piped(search_query: str):
    """
    Piped instance'lar orqali YouTube'dan audio manba topadi.
    Har bir instance'dagi xatoni to'plab, oxirida debug ma'lumot bilan qaytaradi.
    """
    piped_instances = get_active_piped_instances()
    errors = []

    for instance in piped_instances:
        try:
            search_res = requests.get(
                f"{instance}/search",
                params={"q": search_query, "filter": "all"},
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if search_res.status_code != 200:
                errors.append(f"{instance}: search HTTP {search_res.status_code}")
                continue

            items = search_res.json().get("items", [])
            if not items:
                errors.append(f"{instance}: search natijasi bo'sh")
                continue

            video_url = items[0].get("url", "")
            if "v=" not in video_url:
                errors.append(f"{instance}: video_id topilmadi ({video_url})")
                continue
            video_id = video_url.split("v=")[-1]

            streams_res = requests.get(
                f"{instance}/streams/{video_id}",
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if streams_res.status_code != 200:
                errors.append(f"{instance}: streams HTTP {streams_res.status_code}")
                continue

            streams = streams_res.json()
            audio_streams = streams.get("audioStreams", [])

            if not audio_streams:
                errors.append(f"{instance}: audioStreams bo'sh")
                continue

            best_audio = sorted(audio_streams, key=lambda x: x.get("bitrate", 0), reverse=True)[0]
            duration_sec = streams.get("duration", 0)
            mins, secs = divmod(duration_sec, 60)

            print(f"[PIPED] Muvaffaqiyatli: {instance}")
            return {
                "download_url": best_audio.get("url"),
                "duration": f"{mins}:{secs:02d}",
                "bitrate": round(best_audio.get("bitrate", 0) / 1000) if best_audio.get("bitrate") else 128,
                "source_instance": instance,
            }

        except requests.exceptions.Timeout:
            errors.append(f"{instance}: TIMEOUT")
            continue
        except Exception as e:
            errors.append(f"{instance}: EXCEPTION {type(e).__name__} - {str(e)}")
            continue

    print("[PIPED DEBUG] Barcha instance'lar muvaffaqiyatsiz:", errors)
    return {"_debug_errors": errors}


@app.get("/api/v1/convert")
async def convert_spotify_track(
    spotify_url: str = Query(..., description="Spotify track URL"),
    x_rapidapi_proxy_secret: Optional[str] = Header(None, alias="X-RapidAPI-Proxy-Secret"),
):
    if RAPIDAPI_PROXY_SECRET and x_rapidapi_proxy_secret != RAPIDAPI_PROXY_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid Proxy Secret")

    metadata = await asyncio.to_thread(get_spotify_metadata, spotify_url)
    if not metadata:
        raise HTTPException(status_code=400, detail="Invalid Spotify URL or Track not found")

    search_query = f"{metadata['artist']} {metadata['title']}"

    # 1-urinish: yt-dlp (asosiy, tez-tez yangilanadi, vositachi serverga bog'liq emas)
    audio_data = await asyncio.to_thread(get_youtube_audio_ytdlp, search_query)
    all_errors = list(audio_data.get("_debug_errors", [])) if audio_data else []

    # 2-urinish: agar yt-dlp muvaffaqiyatsiz bo'lsa, Piped'ga zaxira sifatida murojaat qilamiz
    if not audio_data or not audio_data.get("download_url"):
        piped_result = await asyncio.to_thread(get_youtube_audio_piped, search_query)
        all_errors += piped_result.get("_debug_errors", []) if piped_result else []
        if piped_result and piped_result.get("download_url"):
            audio_data = piped_result

    if not audio_data or not audio_data.get("download_url"):
        raise HTTPException(
            status_code=404,
            detail=f"Audio source not found. Debug: {all_errors}"
        )

    return {
        "status": "success",
        "data": {
            "title": metadata["title"],
            "artist": metadata["artist"],
            "cover_url": metadata["cover_url"],
            "duration": audio_data["duration"],
            "download_url": audio_data["download_url"],
            "bitrate": audio_data["bitrate"]
        }
    }


@app.get("/health")
async def health_check():
    """Render/uptime monitoring uchun oddiy health-check endpoint."""
    return {"status": "ok"}
