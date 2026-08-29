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
    version="1.5.0"
)

# RapidAPI Proxy Secret - Buni Render/VPS da Environment Variable qilib o'rnating
# Agar o'rnatilmagan bo'lsa, "YOUR_FALLBACK_SECRET" ishlaydi
RAPIDAPI_PROXY_SECRET = os.getenv("RAPIDAPI_PROXY_SECRET", "d90fe7d0-a377-11f1-a0ae-1d5fd5492d49")

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
            return None
            
        soup = BeautifulSoup(response.text, "html.parser")
        script_tag = soup.find("script", id="__NEXT_DATA__")
        
        if script_tag and script_tag.string:
            data = json.loads(script_tag.string)
            # JSON ichidan chuqur qidirish
            try:
                entity = data['props']['pageProps']['state']['data']['entity']
                title = entity.get("title")
                artists = entity.get("artists", [])
                artist_name = ", ".join([a.get("name") for a in artists]) if artists else "Unknown Artist"
                cover_url = entity.get("visualIdentity", {}).get("image", [{}])[0].get("url")
                
                if title:
                    return {"title": title, "artist": artist_name, "cover_url": cover_url}
            except KeyError:
                pass

        # Fallback: Agar NEXT_DATA bo'lmasa meta teglaridan olamiz
        title_tag = soup.find("meta", property="og:title")
        image_tag = soup.find("meta", property="og:image")
        desc_tag = soup.find("meta", property="og:description")

        if title_tag:
            return {
                "title": title_tag["content"],
                "artist": desc_tag["content"].split(" · ")[1] if desc_tag and " · " in desc_tag["content"] else "Unknown Artist",
                "cover_url": image_tag["content"] if image_tag else ""
            }
    except Exception:
        return None
    return None

def get_youtube_audio(search_query: str):
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "nocheckcertificate": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # Qidiruv so'rovini aniqroq qilish
            info = ydl.extract_info(f"ytsearch1:{search_query} official audio", download=False)
            if "entries" in info and len(info["entries"]) > 0:
                video = info["entries"][0]
                return {
                    "download_url": video.get("url"),
                    "duration": video.get("duration_string"),
                    "bitrate": video.get("abr")
                }
        except Exception:
            return None
    return None

@app.get("/api/v1/convert")
async def convert_spotify_track(
    spotify_url: str = Query(..., description="Spotify track URL"),
    x_rapidapi_proxy_secret: Optional[str] = Header(None, alias="X-RapidAPI-Proxy-Secret"),
):
    # 1. Xavfsizlik tekshiruvi (RapidAPI orqali kelayotganiga ishonch hosil qilish)
    if x_rapidapi_proxy_secret != RAPIDAPI_PROXY_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid Proxy Secret")

    # 2. Spotify ma'lumotlarini olish (Blocking callni threadga o'tkazamiz)
    metadata = await asyncio.to_thread(get_spotify_metadata, spotify_url)
    if not metadata:
        raise HTTPException(status_code=400, detail="Invalid Spotify URL or Track not found")

    # 3. Audio manzilini qidirish
    search_query = f"{metadata['artist']} - {metadata['title']}"
    audio_data = await asyncio.to_thread(get_youtube_audio, search_query)

    if not audio_data:
        raise HTTPException(status_code=404, detail="Audio source not found")

    # 4. Natijani qaytarish
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
