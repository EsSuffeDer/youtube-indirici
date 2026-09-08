import os
import sys
import re
import glob
import time
import shutil
import tempfile
import subprocess
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import yt_dlp

app = FastAPI(title="YouTube Akıllı Parça İndirici Web")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Statik dosyaları bağla
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def get_ffmpeg_path() -> str:
    """Sistemdeki veya projede bulunan FFmpeg yolunu bulur."""
    which = shutil.which("ffmpeg")
    if which:
        return which

    # Windows lokal kontrolü
    proj_bin = os.path.join(os.path.dirname(BASE_DIR), "bin", "ffmpeg.exe")
    if os.path.exists(proj_bin):
        return proj_bin

    stacher = os.path.expanduser("~/.stacher/ffmpeg.exe")
    if os.path.exists(stacher):
        return stacher

    return "ffmpeg"


def parse_time_str(time_str: Optional[str]) -> Optional[int]:
    if not time_str:
        return None
    time_str = time_str.strip()
    if not time_str:
        return None
    if re.fullmatch(r"\d+", time_str):
        return int(time_str)
    parts = time_str.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    raise ValueError(f"Geçersiz zaman formatı: {time_str}")


def format_seconds(seconds: Optional[int]) -> str:
    if seconds is None or seconds < 0:
        return "00:00"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


class VideoInfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    format: str = "mp4"          # mp4, mp3, mkv, webm
    resolution: str = "best"     # best, 1080, 720, 480, 360


def cleanup_directory(dir_path: str):
    """İndirme tamamlanıp kullanıcıya gönderildikten sonra geçici klasörü tamamen temizler."""
    try:
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path, ignore_errors=True)
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>YouTube Akıllı Parça İndirici Web</h1><p>index.html bulunamadı.</p>")


@app.get("/manifest.json")
async def serve_manifest():
    manifest_path = os.path.join(STATIC_DIR, "manifest.json")
    if os.path.exists(manifest_path):
        return FileResponse(manifest_path, media_type="application/json")
    raise HTTPException(status_code=404, detail="Manifest bulunamadı")


@app.get("/sw.js")
async def serve_sw():
    sw_path = os.path.join(STATIC_DIR, "sw.js")
    if os.path.exists(sw_path):
        return FileResponse(sw_path, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="Service Worker bulunamadı")


@app.post("/api/info")
async def get_video_info(req: VideoInfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL boş olamaz")

    ffmpeg_bin = get_ffmpeg_path()
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    if os.path.exists(ffmpeg_bin):
        ydl_opts["ffmpeg_location"] = os.path.dirname(ffmpeg_bin)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", "YouTube Videosu"),
                "channel": info.get("uploader", info.get("channel", "YouTube")),
                "duration": info.get("duration", 0),
                "duration_formatted": format_seconds(info.get("duration", 0)),
                "thumbnail": info.get("thumbnail", "")
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Video bilgisi alınamadı: {str(e)[:120]}")


@app.post("/api/download")
async def download_video(req: DownloadRequest, background_tasks: BackgroundTasks):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL boş olamaz")

    try:
        s_sec = parse_time_str(req.start_time)
        e_sec = parse_time_str(req.end_time)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    if s_sec is not None and e_sec is not None and e_sec <= s_sec:
        raise HTTPException(status_code=400, detail="Bitiş süresi başlangıç süresinden büyük olmalıdır.")

    ffmpeg_bin = get_ffmpeg_path()
    is_clip = (s_sec is not None or e_sec is not None)
    temp_dir = tempfile.mkdtemp(prefix="ytdl_web_")

    target_ext = req.format.lower()
    if target_ext not in ("mp4", "mp3", "mkv", "webm"):
        target_ext = "mp4"

    raw_download_tmpl = os.path.join(temp_dir, "raw_video.%(ext)s")

    ydl_opts: Dict[str, Any] = {
        "outtmpl": raw_download_tmpl,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
    }
    if os.path.exists(ffmpeg_bin):
        ydl_opts["ffmpeg_location"] = os.path.dirname(ffmpeg_bin)

    # Format yapılandırması
    if target_ext == "mp3":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = target_ext
        res_map = {"1080": 1080, "720": 720, "480": 480, "360": 360}
        max_h = res_map.get(req.resolution)
        if max_h:
            ydl_opts["format"] = f"bestvideo[height<={max_h}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={max_h}]+bestaudio/best[height<={max_h}]/best"
        else:
            ydl_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=True)
            video_title = sanitize_filename(info_dict.get("title", "video"))

        # İndirilen dosyayı bul
        found = glob.glob(os.path.join(temp_dir, "raw_video.*"))
        if not found:
            raise RuntimeError("İndirilen dosya bulunamadı.")
        downloaded_file = found[0]

        if is_clip:
            s_label = f"{s_sec or 0}s"
            e_label = f"{e_sec}s" if e_sec else "son"
            out_filename = f"{video_title}_[KESIT_{s_label}-{e_label}].{target_ext}"
            clipped_file = os.path.join(temp_dir, out_filename)

            # FFmpeg ile hızlı kesim (stream copy)
            si = None
            if sys.platform == "win32":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE

            cmd = [ffmpeg_bin, "-y"]
            if s_sec is not None:
                cmd += ["-ss", str(s_sec)]
            if e_sec is not None:
                cmd += ["-to", str(e_sec)]
            cmd += ["-i", downloaded_file, "-c", "copy", clipped_file]

            res = subprocess.run(cmd, capture_output=True, text=True, startupinfo=si)
            if res.returncode != 0:
                # Re-encode fallback
                cmd_reencode = [ffmpeg_bin, "-y"]
                if s_sec is not None:
                    cmd_reencode += ["-ss", str(s_sec)]
                if e_sec is not None:
                    cmd_reencode += ["-to", str(e_sec)]
                cmd_reencode += ["-i", downloaded_file, clipped_file]
                res2 = subprocess.run(cmd_reencode, capture_output=True, text=True, startupinfo=si)
                if res2.returncode != 0:
                    raise RuntimeError("FFmpeg kesim hatası.")

            final_file = clipped_file
            send_filename = out_filename
        else:
            final_file = downloaded_file
            send_filename = f"{video_title}.{target_ext}"

        # Yanıt gönderildikten sonra geçici klasörü temizle
        background_tasks.add_task(cleanup_directory, temp_dir)

        # Content-Type belirle
        media_types = {
            "mp4": "video/mp4",
            "mp3": "audio/mpeg",
            "mkv": "video/x-matroska",
            "webm": "video/webm",
        }
        media_type = media_types.get(target_ext, "application/octet-stream")

        import urllib.parse
        encoded_filename = urllib.parse.quote(send_filename)

        return FileResponse(
            path=final_file,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
            }
        )

    except Exception as e:
        cleanup_directory(temp_dir)
        raise HTTPException(status_code=500, detail=f"İndirme başarısız: {str(e)[:120]}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
