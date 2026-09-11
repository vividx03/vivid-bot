import os
import asyncio
import aiohttp
import time
import uuid
import re
import json
import math
import subprocess
from datetime import datetime, timezone, timedelta
from aiohttp import web
from yt_dlp import YoutubeDL
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from PIL import Image, ImageDraw, ImageFont

# ============================================================
# CONFIGURATION — PURELY VIA ENVIRONMENT VARIABLES
# Public repo ke liye sensitive keys yahan hardcode nahi hain
# ============================================================
API_ID_RAW = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not API_ID_RAW or not API_HASH or not BOT_TOKEN:
    raise SystemExit("❌ ERROR: API_ID, API_HASH, ya BOT_TOKEN environment variables missing hain! Hosting dashboard me set karein.")

API_ID = int(API_ID_RAW)

DOWNLOAD_DIR = "./downloads"
DB_FILE = "vivid_db.json"
SCHEDULE_FILE = "schedules.json"

# Telegram max limit: 2GB (Safe boundary: 1.95 GB)
MAX_FILE_SIZE = 1950 * 1024 * 1024  

# Indian Standard Time (+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
for f in [DB_FILE, SCHEDULE_FILE]:
    if not os.path.exists(f):
        with open(f, "w") as fp:
            json.dump({}, fp)

app = Client("vivid_pro_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ============================================================
# GLOBAL DATA
# ============================================================
download_data = {}
last_update_time = {}
url_vault = {}

# ============================================================
# UTILITY FUNCTIONS
# ============================================================
def save_to_db(file_name, thumb_url, duration):
    with open(DB_FILE, "r") as f:
        db = json.load(f)
    db[file_name] = {"thumb": thumb_url, "duration": duration}
    with open(DB_FILE, "w") as f:
        json.dump(db, f)

def get_from_db(file_name):
    with open(DB_FILE, "r") as f:
        db = json.load(f)
    return db.get(file_name, {})

def load_schedules():
    try:
        with open(SCHEDULE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_schedules(data):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_progress_bar(percent):
    done = int(percent / 5)
    remain = 20 - done
    return f"« {'█' * done}{'░' * remain} »"

def TimeFormatter(seconds: int) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

def humanbytes(size):
    if not size: return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0

def get_video_duration(file_path):
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return float(result.stdout.strip())
    except Exception:
        return 0

# ============================================================
# 2GB+ VIDEO SPLITTER (Lossless split via FFmpeg)
# ============================================================
async def split_video(file_path, total_duration, msg=None):
    file_size = os.path.getsize(file_path)
    if file_size <= MAX_FILE_SIZE:
        return [file_path]

    num_parts = math.ceil(file_size / MAX_FILE_SIZE)
    if total_duration <= 0:
        total_duration = get_video_duration(file_path)
    if total_duration <= 0:
        total_duration = 3600

    part_duration = total_duration / num_parts
    base, ext = os.path.splitext(file_path)
    split_files = []

    if msg:
        try: await msg.edit_text(f"✂️ File is {humanbytes(file_size)} (> 2GB).\nSplitting into {num_parts} parts lossless...")
        except: pass

    loop = asyncio.get_event_loop()

    for i in range(num_parts):
        start_time = i * part_duration
        out_part = f"{base}.part{i+1:03d}{ext}"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-i", file_path,
            "-t", str(part_duration),
            "-c", "copy",
            "-map", "0",
            out_part
        ]
        await loop.run_in_executor(None, lambda: subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if os.path.exists(out_part) and os.path.getsize(out_part) > 0:
            split_files.append(out_part)

    return split_files

# ============================================================
# PROGRESS HOOKS (12-second updates)
# ============================================================
def progress_hook(d):
    msg_id = d.get('params', {}).get('msg_id')
    if not msg_id: return
    if d['status'] == 'downloading':
        p = d.get('_percent_str', '0%').replace('%', '').strip()
        try:
            percent = float(p)
        except Exception:
            percent = 0
        download_data[msg_id] = {
            "p": percent,
            "d": d.get('downloaded_bytes', 0),
            "t": d.get('total_bytes') or d.get('total_bytes_estimate', 0),
            "s": d.get('speed', 0),
            "e": d.get('eta', 0)
        }

async def status_manager(message: Message, msg_id: int):
    while True:
        data = download_data.get(msg_id)
        if not data:
            await asyncio.sleep(2)
            continue
        now = time.time()
        if msg_id in last_update_time and (now - last_update_time[msg_id]) < 12:
            await asyncio.sleep(1)
            continue
        last_update_time[msg_id] = now
        bar = get_progress_bar(data['p'])
        status_text = (
            "╔════════════════════╗\n"
            "  ⚡ VIVID DOWNLOADING\n"
            "╚════════════════════╝\n\n"
            f"{bar}\n\n"
            f"📊 PROGRESS: {data['p']}%\n"
            f"📦 SIZE: {humanbytes(data['d'])} / {humanbytes(data['t'])}\n"
            f"🚀 SPEED: {humanbytes(data['s'])}/s\n"
            f"⏳ ETA: {TimeFormatter(data['e'])}\n\n"
            "👨‍💻 DEV: VIVID"
        )
        try:
            await message.edit_text(status_text)
        except Exception:
            pass
        if data['p'] >= 100:
            break
        await asyncio.sleep(10)

async def upload_progress(current, total, msg, start_time, part_tag=""):
    now = time.time()
    if msg.id in last_update_time and (now - last_update_time[msg.id]) < 12:
        return
    last_update_time[msg.id] = now
    diff = now - start_time
    percent = round(current * 100 / total, 1) if total > 0 else 0
    speed = current / diff if diff > 0 else 0
    eta = TimeFormatter((total - current) / speed) if speed > 0 else "0s"
    bar = get_progress_bar(percent)
    text = (
        "╔════════════════════╗\n"
        f"  ⚡ VIVID UPLOADING {part_tag}\n"
        "╚════════════════════╝\n\n"
        f"{bar}\n\n"
        f"📊 UPLOADING: {percent}%\n"
        f"📦 DONE: {humanbytes(current)} / {humanbytes(total)}\n"
        f"🚀 SPEED: {humanbytes(speed)}/s\n"
        f"⏳ ETA: {eta}\n\n"
        "👨‍💻 DEV: VIVID"
    )
    try:
        await msg.edit_text(text)
    except Exception:
        pass

# ============================================================
# THUMBNAIL HANDLERS
# ============================================================
async def download_thumbnail(url):
    if url:
        temp_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.tmp")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        with open(temp_path, "wb") as f:
                            f.write(await resp.read())
                        with Image.open(temp_path) as img:
                            if img.mode in ('RGBA', 'LA', 'P'):
                                img = img.convert('RGB')
                            final_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.jpg")
                            img.save(final_path, "JPEG", quality=95)
                            os.remove(temp_path)
                            if os.path.getsize(final_path) > 0:
                                return final_path
        except Exception as e:
            print(f"Thumbnail error: {e}")
            if os.path.exists(temp_path):
                os.remove(temp_path)
    return await create_fallback_thumbnail()

async def create_fallback_thumbnail():
    file_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.jpg")
    img = Image.new('RGB', (1280, 720), color='#1A1A2E')
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 80)
    except Exception:
        font = ImageFont.load_default()
    draw.text((640, 360), "VIVID", fill="white", anchor="mm", font=font)
    img.save(file_path, "JPEG", quality=95)
    return file_path

# ============================================================
# CORE EXECUTION ENGINE
# ============================================================
async def execute_task(client: Client, chat_id: int, url: str, quality: str, session_info: dict, target_msg: Message):
    msg = target_msg
    raw_title = session_info['title'].split('|')[0].split('-')[0].strip()
    clean_title = re.sub(r'[\\/*?:"<>|]', '', raw_title).strip() or "video"
    file_name = f"{clean_title} vivid.mkv"
    file_path = os.path.join(DOWNLOAD_DIR, file_name)

    save_to_db(file_name, session_info['thumb'], session_info['duration'])

    if quality == "best":
        fmt = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best"
    else:
        fmt = f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={quality}]+bestaudio/best"

    ydl_opts = {
        'format': fmt,
        'outtmpl': file_path,
        'merge_output_format': 'mkv',
        'quiet': True,
        'ignoreerrors': True,
        'no_mtime': True,
        'progress_hooks': [progress_hook],
        'params': {'msg_id': msg.id},
        'retries': 20,
        'fragment_retries': 20,
        'concurrent_fragments': 16,
        'throttled_rate': 1000000,
        'no_check_certificate': True,
        'no_part': True,
        'hls_prefer_native': True,
        'live_from_start': True,
        'wait_for_video': (1, 60),
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        },
    }

    thumb_path = None
    created_parts = []
    try:
        asyncio.create_task(status_manager(msg, msg.id))
        thumb_path = await download_thumbnail(session_info['thumb'])

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: YoutubeDL(ydl_opts).download([url]))

        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            raise Exception("Downloaded file is missing or empty.")

        video_dur = int(session_info.get('duration') or get_video_duration(file_path))
        upload_queue = await split_video(file_path, video_dur, msg)

        total_parts = len(upload_queue)
        for idx, current_file in enumerate(upload_queue, start=1):
            if current_file != file_path:
                created_parts.append(current_file)

            part_tag = f"[{idx}/{total_parts}]" if total_parts > 1 else ""
            await msg.edit_text(f"📡 `Uploading Payload {part_tag}...`")
            start_time = time.time()
            last_update_time[msg.id] = start_time

            part_caption = f"{session_info['title']}"
            if total_parts > 1:
                part_caption += f" — Part {idx}/{total_parts}"
            part_caption += f"\n\n[{session_info['uploader']}]\n\n[𝐂𝐑𝐄𝐃𝐈𝐓 : 𝐕𝐈𝐕𝐈𝐃 🤍](https://whatsapp.com/channel/0029VbDx2j1BadmU3wdvsv31)"

            p_dur = int(video_dur / total_parts) if total_parts > 1 else video_dur

            await client.send_video(
                chat_id=chat_id,
                video=current_file,
                caption=part_caption,
                thumb=thumb_path,
                duration=p_dur,
                progress=upload_progress,
                progress_args=(msg, start_time, part_tag)
            )
            await asyncio.sleep(2)

        await msg.delete()

    except Exception as e:
        error_msg = f"❌ ERROR: {str(e)}"
        try: await msg.edit_text(error_msg)
        except Exception: pass
    finally:
        if thumb_path and os.path.exists(thumb_path):
            try: os.remove(thumb_path)
            except: pass
        if os.path.exists(file_path):
            try: os.remove(file_path)
            except: pass
        for p in created_parts:
            if os.path.exists(p):
                try: os.remove(p)
                except: pass
        download_data.pop(msg.id, None)

# ============================================================
# BACKGROUND SCHEDULER
# ============================================================
async def scheduler_worker(client: Client):
    while True:
        try:
            schedules = load_schedules()
            now_ts = datetime.now(timezone.utc).timestamp()
            triggered_any = False

            for job_id, job in list(schedules.items()):
                if not job.get("done") and now_ts >= job["start_timestamp"]:
                    job["done"] = True
                    save_schedules(schedules)
                    triggered_any = True
                    print(f"⏰ [Scheduler] Triggering scheduled download for: {job['title']}")

                    try:
                        start_msg = await client.send_message(
                            job["chat_id"],
                            f"🔔 **Scheduled Live Stream Started!**\n\n🎬 `{job['title']}`\n⚡ Starting download automatically..."
                        )
                        last_update_time[start_msg.id] = time.time()
                        asyncio.create_task(
                            execute_task(
                                client=client,
                                chat_id=job["chat_id"],
                                url=job["url"],
                                quality=job.get("quality", "best"),
                                session_info=job,
                                target_msg=start_msg
                            )
                        )
                    except Exception as err:
                        print(f"Scheduler execution error: {err}")

            if triggered_any:
                save_schedules(schedules)
        except Exception as e:
            print(f"Scheduler loop error: {e}")

        await asyncio.sleep(30)

# ============================================================
# COMMANDS & HANDLERS
# ============================================================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    text = """```
╔══════════════════════════════════╗
        VIVID DOWNLOADER ENGINE
╚══════════════════════════════════╝

Status : ONLINE ✅

🎬 YouTube Videos Supported
📡 Live Stream Capture Enabled
📅 Auto-Schedule Upcoming Live Streams
✂️ 2GB+ Auto-Split Support Active
🚀 Ultra Fast Download System

Developed & Maintained By : VIVID
System Ready For Commands...
```"""
    await message.reply_text(text)

@app.on_message(filters.command("mylive"))
async def mylive_cmd(client, message):
    schedules = load_schedules()
    active = [v for v in schedules.values() if v.get("chat_id") == message.chat.id and not v.get("done")]
    if not active:
        await message.reply_text("📭 Koi scheduled stream queued nahi hai.")
        return

    res = "📅 **Aapki Scheduled Streams:**\n\n"
    for item in active:
        start_dt = datetime.fromtimestamp(item["start_timestamp"], IST)
        res += f"🎬 **{item['title']}**\n⏰ Start Time: `{start_dt.strftime('%d %b %Y, %I:%M %p IST')}`\n🔗 `{item['url']}`\n\n"
    await message.reply_text(res)

@app.on_message(filters.command("uploaddd"))
async def bulk_upload(client, message):
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith((".mkv", ".mp4"))]
    if not files:
        await message.reply_text("No downloaded files found in the vault.")
        return
    await message.reply_text(f"Found {len(files)} files. Starting Re-Upload Engine...")
    for file_name in files:
        file_path = os.path.join(DOWNLOAD_DIR, file_name)
        info = get_from_db(file_name)
        tmp = await message.reply_text(f"📡 `Preparing Payload: {file_name}`")
        start_time = time.time()
        last_update_time[tmp.id] = start_time
        thumb_path = await download_thumbnail(info.get("thumb"))
        try:
            await client.send_video(
                chat_id=message.chat.id,
                video=file_path,
                caption=f"✅ `{file_name}` (Re-Uploaded)",
                thumb=thumb_path,
                duration=int(info.get("duration", 0)),
                progress=upload_progress,
                progress_args=(tmp, start_time, "")
            )
            await tmp.delete()
            await asyncio.sleep(2)
        except Exception as e:
            await tmp.edit_text(f"❌ Failed to upload {file_name}: {str(e)}")
        finally:
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)

# --- LINK HANDLER ---
@app.on_message(filters.regex(r"https?://(www\.)?youtube\.com|youtu\.be"))
async def link_handler(client, message):
    url = message.text.strip()
    try: await message.delete()
    except Exception: pass

    tmp = await message.reply_text("Initializing...")
    ydl_opts = {'quiet': True, 'no_warnings': True}

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: YoutubeDL(ydl_opts).extract_info(url, download=False))
        session_id = str(uuid.uuid4())[:8]

        thumb_url = info.get('thumbnail')
        if not thumb_url and info.get('thumbnails'):
            thumbnails = sorted(info['thumbnails'], key=lambda x: x.get('width', 0) * x.get('height', 0), reverse=True)
            if thumbnails:
                thumb_url = thumbnails[0].get('url')
        if not thumb_url:
            thumb_url = "https://img.youtube.com/vi/" + info.get('id', '') + "/maxresdefault.jpg"

        title = info.get('title', 'video')
        uploader = info.get('uploader') or info.get('channel') or "Unknown"
        duration = info.get('duration', 0)
        release_timestamp = info.get('release_timestamp')

        is_upcoming = info.get('live_status') == 'is_upcoming' or (release_timestamp and release_timestamp > time.time())

        if is_upcoming and release_timestamp:
            start_dt = datetime.fromtimestamp(release_timestamp, IST)
            schedules = load_schedules()
            schedules[session_id] = {
                "chat_id": message.chat.id,
                "url": url,
                "title": title,
                "uploader": uploader,
                "thumb": thumb_url,
                "duration": duration,
                "quality": "best",
                "start_timestamp": release_timestamp,
                "done": False
            }
            save_schedules(schedules)

            msg_text = (
                "╔══════════════════════════════════╗\n"
                "  ⏰ UPCOMING LIVE DETECTED\n"
                "╚══════════════════════════════════╝\n\n"
                f"🎬 **Title:** `{title}`\n"
                f"👤 **Channel:** `{uploader}`\n"
                f"📅 **Start Time:** `{start_dt.strftime('%d %b %Y, %I:%M %p IST')}`\n\n"
                "✅ **Auto-Schedule Added!**\n"
                "Aapko kuch karne ki zarurat nahi hai. Jaise hi ye stream live aayegi, bot turant apne aap isko record/download karke upload kar dega!\n\n"
                "📌 View list anytime with `/mylive`"
            )
            await tmp.edit_text(msg_text)
            return

        url_vault[session_id] = {
            "url": url,
            "title": title,
            "uploader": uploader,
            "thumb": thumb_url,
            "duration": duration
        }

        buttons = [
            [InlineKeyboardButton("480p", callback_data=f"q|480|{session_id}"),
             InlineKeyboardButton("720p", callback_data=f"q|720|{session_id}")],
            [InlineKeyboardButton("1080p", callback_data=f"q|1080|{session_id}"),
             InlineKeyboardButton("Best Quality", callback_data=f"q|best|{session_id}")]
        ]
        await tmp.edit_text(
            f"🎬 **Title:** `{title}`\n\nSelect desired quality:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        await tmp.edit_text(f"❌ ERROR: {str(e)}")

# --- DOWNLOAD CALLBACK ---
@app.on_callback_query(filters.regex(r"^q\|"))
async def download_callback(client: Client, callback_query: CallbackQuery):
    _, quality, session_id = callback_query.data.split("|")
    data = url_vault.get(session_id)
    if not data:
        await callback_query.answer("Session Expired!", show_alert=True)
        return

    msg = callback_query.message
    await msg.edit_text("⚡ `Initializing Kernel...`")
    last_update_time[msg.id] = time.time()

    await execute_task(
        client=client,
        chat_id=msg.chat.id,
        url=data['url'],
        quality=quality,
        session_info=data,
        target_msg=msg
    )

# ============================================================
# DUMMY HEALTH SERVER (Render/Railway Sleep Preventer)
# ============================================================
async def health_server():
    async def handle(request):
        return web.Response(text="Vivid Bot is Online 24/7 🚀")
    server = web.Application()
    server.router.add_get('/', handle)
    runner = web.AppRunner(server)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Health check server listening on port {port}")

# ============================================================
# MAIN ENTRYPOINT
# ============================================================
async def main():
    asyncio.create_task(health_server())
    await app.start()
    me = await app.get_me()
    print(f"🚀 Bot is running 24/7 as @{me.username}")
    asyncio.create_task(scheduler_worker(app))
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
