import json
import os
import re
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Query
import requests
import yt_dlp

app = FastAPI(title="Spotify Audio Extractor API")

# RapidAPI Proxy Secret kalitini bu yerga yozing
# Yoki Render Environment Variable qilib kiritganingiz ma'qul (masalan: os.getenv("RAPIDAPI_PROXY_SECRET"))
RAPIDAPI_PROXY_SECRET = os.getenv(
    "RAPIDAPI_PROXY_SECRET", "d90fe7d0-a377-11f1-a0ae-1d5fd5492d49"
)


def extract_track_id(url: str) -> str:
    match = re.search(r"track/([a-zA-Z0-9]+)", url)
    if match:
        return match.group(1)
    return None


def get_spotify_metadata(spotify_url: str):
    track_id = extract_track_id(spotify_url)
    if not track_id:
        return None

    embed_url = f"https://open.spotify.com/embed/track/{track_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        response = requests.get(embed_url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")

            script_tag = soup.find("script", id="__NEXT_DATA__")
            if script_tag and script_tag.string:
                data = json.loads(script_tag.string)
                entity = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("state", {})
                    .get("data", {})
                    .get("entity", {})
                )

                if entity:
                    title = entity.get("title")
                    artists = entity.get("artists", [])
                    artist_name = (
                        ", ".join([a.get("name") for a in artists])
                        if artists
                        else "Unknown Artist"
                    )
                    cover_url = entity.get("visualIdentity", {}).get(
                        "image", [{}
                    ])[0].get("url")

                    if title:
                        return {
                            "title": title,
                            "artist": artist_name,
                            "cover_url": cover_url,
                        }

            title_tag = soup.find("meta", property="og:title")
            desc_tag = soup.find("meta", property="og:description")
            image_tag = soup.find("meta", property="og:image")

            title = title_tag["content"] if title_tag else None
            cover_url = image_tag["content"] if image_tag else None
            artist = "Unknown Artist"

            if desc_tag and desc_tag.get("content"):
                artist = desc_tag["content"]

            if title:
                return {
                    "title": title,
                    "artist": artist,
                    "cover_url": cover_url,
                }
    except Exception:
        pass

    return None


def get_youtube_audio(search_query: str):
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch1:{search_query}", download=False)
            if "entries" in info and len(info["entries"]) > 0:
                video = info["entries"][0]
                return {
                    "download_url": video.get("url"),
                    "duration": video.get("duration_string"),
                }
        except Exception:
            return None

    return None


@app.get("/api/v1/convert")
def convert_spotify_track(
    spotify_url: str = Query(..., description="Spotify track URL"),
    x_rapidapi_proxy_secret: str = Header(None, alias="X-RapidAPI-Proxy-Secret"),
):
    # RapidAPI orqali kelmagan to'g'ridan-to'g'ri so'rovlarni bloklash
    if (
        RAPIDAPI_PROXY_SECRET
        and RAPIDAPI_PROXY_SECRET != "Sening_RapidAPI_Proxy_Secret_Kaliting"
    ):
        if x_rapidapi_proxy_secret != RAPIDAPI_PROXY_SECRET:
            raise HTTPException(
                status_code=403,
                detail="Ruxsat berilmadi: Ushbu API faqat RapidAPI orqali ishlaydi",
            )

    if "spotify.com/track/" not in spotify_url:
        raise HTTPException(
            status_code=400, detail="Yaroqsiz Spotify track havolasi"
        )

    # 1. Spotify metama'lumotlarini olish
    metadata = get_spotify_metadata(spotify_url)
    if not metadata or not metadata.get("title"):
        raise HTTPException(
            status_code=404,
            detail="Spotify trek ma'lumotlarini olib bo'mladi",
        )

    title = metadata["title"]
    artist = metadata["artist"]
    cover_url = metadata["cover_url"]

    # 2. Qidiruv so'rovi
    query = (
        f"{artist} - {title}"
        if artist != "Unknown Artist"
        else title
    )

    # 3. YouTube Audio URL va davomiylikni olish
    yt_data = get_youtube_audio(query)

    download_url = yt_data.get("download_url") if yt_data else None
    duration = yt_data.get("duration") if yt_data else "00:00"

    return {
        "status": "success",
        "data": {
            "title": title,
            "artist": artist,
            "cover_url": cover_url,
            "duration": duration,
            "download_url": download_url,
        },
    }
