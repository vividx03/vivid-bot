import os
import asyncio
import aiohttp
import time
import uuid
import re
import json
from datetime import datetime, timedelta
from aiohttp import web
from yt_dlp import YoutubeDL
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from PIL import Image, ImageDraw, ImageFont

# ============================================================
# CONFIG — SAB ENV VARIABLES SE (Public repo mein kuch nahi)
# ============================================================
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

DOWNLOAD_DIR = "./downloads"
DB_FILE = "vivid_db.json"
SCHEDULE_FILE = "schedules.json"
TIMEZONE_OFFSET = int(os.environ.get("TZ_OFFSET", "5"))  # IST = +5

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise SystemExit("❌ API_ID / API_HASH / BOT_TOKEN env vars missing!")

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
# UTILS
# ============================================================
def load_schedules():
    with open(SCHEDULE_FILE, "r") as f:
        return json.load(f)

def save_schedules(data):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(data, f)

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
    return f"{seconds}s"

def humanbytes(size):
    if not size: return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0

def now_local():
    """Current time in configured timezone (naive datetime)"""
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)

# ============================================================
# PROGRESS HOOKS
# ============================================================
def progress_hook(d):
    msg_id = d.get('params', {}).get('msg_id')
    if not msg_id: return
    if d['status'] == 'downloading':
        p = d.get('_percent_str', '0%').replace('%', '').strip()
        try: percent = float(p)
        except: percent = 0
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
        text = (
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
        try: await message.edit_text(text)
        except: pass
        if data['p'] >= 100: break
        await asyncio.sleep(10)

async def upload_progress(current, total, msg, start_time):
    now = time.time()
    if msg.id in last_update_time and (now - last_update_time[msg.id]) < 12:
        return
    last_update_time[msg.id] = now
    diff = now - start_time
    percent = round(current * 100 / total, 1)
    speed = current / diff if diff > 0 else 0
    eta = TimeFormatter((total - current) / speed) if speed > 0 else "0s"
    bar = get_progress_bar(percent)
    text = (
        "╔════════════════════╗\n"
        "  ⚡ VIVID UPLOADING\n"
        "╚════════════════════╝\n\n"
        f"{bar}\n\n"
        f"📊 UPLOADING: {percent}%\n"
        f"📦 DONE: {humanbytes(current)} / {humanbytes(total)}\n"
        f"🚀 SPEED: {humanbytes(speed)}/s\n"
        f"⏳ ETA: {eta}\n\n"
        "👨‍💻 DEV: VIVID"
    )
    try: await msg.edit_text(text)
    except: pass

# ============================================================
# THUMBNAIL
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
                            return final_path
        except Exception as e:
            print(f"Thumb error: {e}")
            if os.path.exists(temp_path):
                os.remove(temp_path)
    return await create_fallback_thumbnail()

async def create_fallback_thumbnail():
    file_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.jpg")
    img = Image.new('RGB', (1280, 720), color='#1A1A2E')
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 80)
    except:
        font = ImageFont.load_default()
    draw.text((640, 360), "VIVID", fill="white", anchor="mm", font=font)
    img.save(file_path, "JPEG", quality=95)
    return file_path

# ============================================================
# CORE DOWNLOAD & UPLOAD (reusable)
# ============================================================
async def process_download(client, chat_id, url, quality, title_hint=None, reply_msg_id=None):
    ydl_probe = {'quiet': True, 'no_warnings': True}
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, lambda: YoutubeDL(ydl_probe).extract_info(url, download=False))

    title = info.get('title', title_hint or 'video')
    duration = info.get('duration', 0)
    uploader = info.get('uploader') or info.get('channel') or "Unknown"
    thumb_url = info.get('thumbnail')
    if not thumb_url and info.get('thumbnails'):
        ts = sorted(info['thumbnails'], key=lambda x: x.get('width', 0) * x.get('height', 0), reverse=True)
        if ts: thumb_url = ts[0].get('url')
    if not thumb_url:
        thumb_url = "https://img.youtube.com/vi/" + info.get('id', '') + "/maxresdefault.jpg"

    if reply_msg_id:
        msg = await client.edit_message_text(chat_id, reply_msg_id, "⚡ `Initializing Kernel...`")
    else:
        msg = await client.send_message(chat_id, "⚡ `Initializing Kernel...`")

    last_update_time[msg.id] = time.time()

    raw_title = title.split('|')[0].split('-')[0].strip()
    clean_title = re.sub(r'[\\/*?:"<>|]', '', raw_title).strip() or "video"
    file_name = f"{clean_title} vivid.mkv"
    file_path = os.path.join(DOWNLOAD_DIR, file_name)

    save_to_db(file_name, thumb_url, duration)

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
    try:
        asyncio.create_task(status_manager(msg, msg.id))
        thumb_path = await download_thumbnail(thumb_url)
        await loop.run_in_executor(None, lambda: YoutubeDL(ydl_opts).download([url]))

        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            raise Exception("Downloaded file missing or empty.")

        await asyncio.sleep(2)
        await msg.edit_text("📡 `Uploading Payload...`")

        start_time = time.time()
        last_update_time[msg.id] = start_time

        caption = f"{title}\n\n[{uploader}]\n\n[𝐂𝐑𝐄𝐃𝐈𝐓 : 𝐕𝐈𝐕𝐈𝐃 🤍](https://whatsapp.com/channel/0029VbDx2j1BadmU3wdvsv31)"

        await client.send_video(
            chat_id=chat_id,
            video=file_path,
            caption=caption,
            thumb=thumb_path,
            duration=int(duration),
            progress=upload_progress,
            progress_args=(msg, start_time)
        )
        await msg.delete()
    except Exception as e:
        try: await msg.edit_text(f"❌ ERROR: {str(e)}")
        except: pass
    finally:
        if thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)
        download_data.pop(msg.id, None)

# ============================================================
# SCHEDULER — Background loop
# ============================================================
async def scheduler_loop(client: Client):
    """Har 30 sec check karo, time aaya toh auto-download chalao."""
    while True:
        try:
            schedules = load_schedules()
            now = now_local()
            due_ids = []
            for job_id, job in schedules.items():
                run_at = datetime.fromisoformat(job['run_at'])
                if now >= run_at and not job.get('done'):
                    due_ids.append((job_id, job))

            for job_id, job in due_ids:
                print(f"⏰ Running scheduled job: {job_id}")
                schedules[job_id]['done'] = True
                save_schedules(schedules)
                try:
                    await client.send_message(
                        job['chat_id'],
                        f"⏰ **Scheduled Live Started!**\n\n🎬 {job['title']}\n📥 Quality: {job['quality']}"
                    )
                    await process_download(
                        client, job['chat_id'], job['url'], job['quality'],
                        title_hint=job['title']
                    )
                except Exception as e:
                    print(f"Schedule job error: {e}")
                    try:
                        await client.send_message(job['chat_id'], f"❌ Scheduled job failed: {e}")
                    except: pass

            # Cleanup done jobs older than 1 day
            schedules = load_schedules()
            to_del = [k for k, v in schedules.items()
                      if v.get('done') and (now - datetime.fromisoformat(v['run_at'])).total_seconds() > 86400]
            for k in to_del:
                del schedules[k]
            if to_del:
                save_schedules(schedules)
        except Exception as e:
            print(f"Scheduler loop error: {e}")
        await asyncio.sleep(30)

# ============================================================
# HANDLERS
# ============================================================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    text = """```
╔══════════════════════════════════╗
        VIVID DOWNLOADER ENGINE
╚══════════════════════════════════╝

Status : ONLINE ✅

🎬 YouTube Video Download
📅 Schedule Future Live Download
🚀 Ultra Fast System

Commands:
/live <link> YYYY-MM-DD HH:MM  → schedule
/live <link> HH:MM             → aaj/kal ka time
/mylive                        → list scheduled
/cancel_live <id>              → cancel
/uploaddd                      → re-upload old files

Developed By : VIVID
```"""
    await message.reply_text(text)

@app.on_message(filters.regex(r"https?://(www\.)?youtube\.com|youtu\.be"))
async def link_handler(client, message):
    url = message.text.strip()
    try: await message.delete()
    except: pass
    tmp = await message.reply_text("Initializing...")
    ydl_opts = {'quiet': True, 'no_warnings': True}
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: YoutubeDL(ydl_opts).extract_info(url, download=False))
        session_id = str(uuid.uuid4())[:8]
        thumb_url = info.get('thumbnail')
        if not thumb_url and info.get('thumbnails'):
            ts = sorted(info['thumbnails'], key=lambda x: x.get('width', 0) * x.get('height', 0), reverse=True)
            if ts: thumb_url = ts[0].get('url')
        if not thumb_url:
            thumb_url = "https://img.youtube.com/vi/" + info.get('id', '') + "/maxresdefault.jpg"
        url_vault[session_id] = {
            "url": url,
            "title": info.get('title', 'video'),
            "uploader": info.get('uploader') or info.get('channel') or "Unknown",
            "thumb": thumb_url,
            "duration": info.get('duration', 0)
        }
        buttons = [
            [InlineKeyboardButton("480p", callback_data=f"q|480|{session_id}"),
             InlineKeyboardButton("720p", callback_data=f"q|720|{session_id}")],
            [InlineKeyboardButton("1080p", callback_data=f"q|1080|{session_id}"),
             InlineKeyboardButton("Best Quality", callback_data=f"q|best|{session_id}")]
        ]
        await tmp.edit_text(
            f"🎬 **Title:** `{info.get('title')}`\n\nSelect desired quality:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        await tmp.edit_text(f"❌ ERROR: {str(e)}")

@app.on_message(filters.command("live"))
async def schedule_live(client, message):
    """Usage:
       /live <url> YYYY-MM-DD HH:MM
       /live <url> HH:MM   (aaj ya kal)
    """
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            "📝 **Usage:**\n\n"
            "`/live <youtube_link> YYYY-MM-DD HH:MM`\n"
            "Example: `/live https://youtu.be/xxxx 2026-03-15 09:00`\n\n"
            "**Shortcut (aaj/kal):**\n"
            "`/live <link> 9:00` → aaj 9 baje (guzar gaya toh kal)\n"
            "`/live <link> 21:30` → aaj 9:30 PM\n"
        )
        return

    parts = args[1].strip().split()
    if len(parts) < 2:
        await message.reply_text("❌ Link ya time missing. `/live` for help.")
        return

    url = parts[0]
    if not re.match(r"https?://", url):
        await message.reply_text("❌ Invalid URL.")
        return

    time_str = " ".join(parts[1:])
    run_at = None
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}$", time_str):
            run_at = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        elif re.match(r"^\d{1,2}:\d{2}$", time_str):
            h, m = map(int, time_str.split(":"))
            now = now_local()
            candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            run_at = candidate
        else:
            raise ValueError("Bad format")
    except Exception:
        await message.reply_text("❌ Time format galat. `YYYY-MM-DD HH:MM` ya `HH:MM` use karo.")
        return

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: YoutubeDL({'quiet': True}).extract_info(url, download=False))
        title = info.get('title', 'Live Stream')
    except Exception as e:
        await message.reply_text(f"⚠️ Info fetch failed: {e}\nSaving as 'Live Stream'")
        title = "Live Stream"

    job_id = str(uuid.uuid4())[:8]
    schedules = load_schedules()
    schedules[job_id] = {
        "chat_id": message.chat.id,
        "url": url,
        "quality": "best",
        "run_at": run_at.isoformat(),
        "title": title,
        "done": False
    }
    save_schedules(schedules)

    await message.reply_text(
        f"✅ **Live Scheduled!**\n\n"
        f"🆔 ID: `{job_id}`\n"
        f"🎬 {title}\n"
        f"📅 Start: `{run_at.strftime('%Y-%m-%d %H:%M')}`\n"
        f"📥 Quality: Best\n\n"
        f"Bot exact time pe auto-download karega. `/mylive` se list dekho."
    )

@app.on_message(filters.command("mylive"))
async def my_live(client, message):
    schedules = load_schedules()
    mine = {k: v for k, v in schedules.items() if v['chat_id'] == message.chat.id and not v.get('done')}
    if not mine:
        await message.reply_text("📭 Koi scheduled live nahi hai.")
        return
    lines = ["📅 **Your Scheduled Lives:**\n"]
    for jid, j in mine.items():
        dt = datetime.fromisoformat(j['run_at'])
        lines.append(f"`{jid}` → {j['title'][:40]}\n   ⏰ {dt.strftime('%Y-%m-%d %H:%M')}\n")
    await message.reply_text("\n".join(lines))

@app.on_message(filters.command("cancel_live"))
async def cancel_live(client, message):
    args = message.text.split()
    if len(args) < 2:
        await message.reply_text("Usage: `/cancel_live <job_id>`")
        return
    jid = args[1]
    schedules = load_schedules()
    if jid not in schedules:
        await message.reply_text("❌ ID not found.")
        return
    if schedules[jid]['chat_id'] != message.chat.id:
        await message.reply_text("❌ Ye tumhara schedule nahi hai.")
        return
    del schedules[jid]
    save_schedules(schedules)
    await message.reply_text(f"✅ Cancelled `{jid}`")

@app.on_callback_query(filters.regex(r"^q\|"))
async def download_callback(client: Client, callback_query: CallbackQuery):
    _, quality, session_id = callback_query.data.split("|")
    data = url_vault.get(session_id)
    if not data:
        await callback_query.answer("Session Expired!", show_alert=True)
        return
    await callback_query.message.delete()
    await process_download(client, callback_query.message.chat.id, data['url'], quality, title_hint=data['title'])

@app.on_message(filters.command("uploaddd"))
async def bulk_upload(client, message):
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith((".mkv", ".mp4"))]
    if not files:
        await message.reply_text("No files.")
        return
    await message.reply_text(f"Found {len(files)} files. Re-uploading...")
    for file_name in files:
        file_path = os.path.join(DOWNLOAD_DIR, file_name)
        info = get_from_db(file_name)
        tmp = await message.reply_text(f"📡 `Preparing: {file_name}`")
        start_time = time.time()
        last_update_time[tmp.id] = start_time
        thumb_path = await download_thumbnail(info.get("thumb"))
        try:
            await client.send_video(
                chat_id=message.chat.id, video=file_path,
                caption=f"✅ `{file_name}`",
                thumb=thumb_path, duration=int(info.get("duration", 0)),
                progress=upload_progress, progress_args=(tmp, start_time)
            )
            await tmp.delete()
        except Exception as e:
            await tmp.edit_text(f"❌ {e}")
        finally:
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)

# ============================================================
# HEALTH SERVER (Render ke liye — sleep rok)
# ============================================================
async def health_server():
    async def handle(request):
        return web.Response(text="Vivid Bot is alive ✅")
    http_app = web.Application()
    http_app.router.add_get('/', handle)
    runner = web.AppRunner(http_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Health server on :{port}")

# ============================================================
# MAIN — Bot + Scheduler + Health ek saath
# ============================================================
async def main():
    asyncio.create_task(health_server())
    await app.start()
    me = await app.get_me()
    print(f"🚀 Vivid Bot online: @{me.username}")
    asyncio.create_task(scheduler_loop(app))
    print("⏰ Scheduler started")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
