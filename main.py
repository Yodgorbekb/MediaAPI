import json
import os
import re
import asyncio
from typing import Optional
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Query
import requests

app = FastAPI(
    title="Spotify Audio Extractor API",
    description="Professional tool to get metadata and audio from Spotify links.",
    version="1.6.0"
)

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

def get_youtube_audio_piped(search_query: str):
    # Public Piped API lari ro'yxati (zaxira bilan)
    piped_instances = [
        "https://pipedapi.kavin.rocks",
        "https://api.piped.privacydev.net",
        "https://pipedapi.drgns.space"
    ]

    for instance in piped_instances:
        try:
            # 1. YouTube bo'yicha qidirish
            search_res = requests.get(
                f"{instance}/search?q={search_query}&filter=all",
                timeout=5
            )
            if search_res.status_code != 200:
                continue

            items = search_res.json().get("items", [])
            if not items:
                continue

            video_id = items[0]["url"].split("v=")[-1]

            # 2. Audio oqimini olish
            streams_res = requests.get(
                f"{instance}/streams/{video_id}",
                timeout=5
            )
            if streams_res.status_code != 200:
                continue

            streams = streams_res.json()
            audio_streams = streams.get("audioStreams", [])
            
            if audio_streams:
                # Bitrate bo'yicha eng yaxshi audioni tanlash
                best_audio = sorted(audio_streams, key=lambda x: x.get("bitrate", 0), reverse=True)[0]
                
                duration_sec = streams.get("duration", 0)
                mins, secs = divmod(duration_sec, 60)
                duration_str = f"{mins}:{secs:02d}"

                return {
                    "download_url": best_audio.get("url"),
                    "duration": duration_str,
                    "bitrate": round(best_audio.get("bitrate", 0) / 1000) if best_audio.get("bitrate") else 128
                }
        except Exception as e:
            print(f"Error with instance {instance}:", e)
            continue

    return None

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
    audio_data = await asyncio.to_thread(get_youtube_audio_piped, search_query)

    if not audio_data:
        raise HTTPException(status_code=404, detail="Audio source not found")

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
