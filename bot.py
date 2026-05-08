import asyncio
import time
import math
import re
import os
import sys
import json

# Python 3.14 compatibility — Pyrogram 2.0.106 needs an event loop at import time
asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters
from pyrogram import raw
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from hypertg import HyperTGDownload, HyperTGUpload

# 👇 Environment variables from Heroku config (via config.py)
from config import Config

app = Client("EncodeBot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN)

user_settings = {}
premium_users = [Config.ADMIN_ID] # Admin automatically premium hai
video_queue = asyncio.Queue()
worker_running = False
cancel_flags = {}
waiting_for = {}
last_media = {}  # per user last media message store karega
current_processing = {"user_id": None, "start_time": 0, "res": None}
queue_list = []  # (user_id, timestamp, res_tag) for /queue tracking
task_progress = {}  # user_id -> {phase, percent, speed, processed, total, eta, elapsed, filename, gid}

async def set_bot_menu():
    await app.set_bot_commands([
        BotCommand("start", "Bot start karein"),
        BotCommand("encode", "Video encode karein 🎬"),
        BotCommand("ec", "Video encode karein (shortcut)"),
        BotCommand("my_status", "Premium status check karein"),
        BotCommand("settings", "Settings (Premium Only)"),
        BotCommand("set_mode", "Set Mode (Premium Only)"),
        BotCommand("set_codec", "Set Codec (Premium Only)"),
        BotCommand("set_preset", "Set Preset (Premium Only)"),
        BotCommand("set_crf", "Set CRF (Premium Only)"),
        BotCommand("set_audio", "Set Audio (Premium Only)"),
        BotCommand("queue", "Queue position check karein 📋"),
        BotCommand("cancel", "Process cancel karein ❌")
    ])

def get_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = {
            "srt": None, "thumb": None, "codec": "libx264", "preset": "fast", "mode": "all",
            "crf": {"360p": 28, "480p": 26, "720p": 24, "1080p": 22},
            "audio": {"360p": "48k", "480p": "64k", "720p": "96k", "1080p": "128k"},
            "_template": True
        }
    return user_settings[user_id]

def get_cancel_button(user_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Process", callback_data=f"cancel_{user_id}")]])

def format_time(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h}h {m}m {s}s"
    elif m > 0: return f"{m}m {s}s"
    else: return f"{s}s"

def leech_bar(percent):
    filled = "🟦" * int(percent / 5)
    empty = "🟧" * (20 - int(percent / 5))
    return filled + empty

def is_premium(user_id, chat_id=None):
    if chat_id == Config.AUTH_GC:
        return True
    return user_id in premium_users

# --- ADMIN COMMANDS ---
@app.on_message(filters.command("add_pre") & filters.user(Config.ADMIN_ID))
async def add_prem(client, message: Message):
    if len(message.command) == 2:
        try:
            u_id = int(message.command[1])
            if u_id not in premium_users:
                premium_users.append(u_id)
                await message.reply_text(f"✅ User `{u_id}` ko Premium access mil gaya hai!")
                try: await client.send_message(u_id, "🎉 Congratulations! Aapko Admin dwara **Premium Access** mil gaya hai. Ab aap Encode bot use kar sakte hain!")
                except: pass
            else: await message.reply_text("Ye user pehle se premium hai.")
        except: await message.reply_text("Sahi format: `/add_pre 123456789`")

@app.on_message(filters.command("remove_pre") & filters.user(Config.ADMIN_ID))
async def rem_prem(client, message: Message):
    if len(message.command) == 2:
        u_id = int(message.command[1])
        if u_id in premium_users and u_id != Config.ADMIN_ID:
            premium_users.remove(u_id)
            await message.reply_text(f"❌ User `{u_id}` ka Premium access hata diya gaya hai.")
        else: await message.reply_text("User list me nahi hai ya wo Admin hai.")

@app.on_message(filters.command("my_status"))
async def my_status(client, message: Message):
    if is_premium(message.from_user.id, message.chat.id):
        await message.reply_text(f"👤 **{message.from_user.first_name}**, Aap ek **PREMIUM MEMBER ✨** hain!\nAap bot ki saari services use kar sakte hain.")
    else:
        await message.reply_text(f"👤 **{message.from_user.first_name}**, Aap abhi **FREE MEMBER ❌** hain.\nEncode karne ke liye Premium lena hoga. Admin se sampark karein.")

# --- CANCEL & PROGRESS BARS ---
@app.on_callback_query(filters.regex(r"^cancel_"))
async def cancel_cb(client, callback_query):
    uid = callback_query.from_user.id
    if str(uid) in callback_query.data:
        cancel_flags[uid] = True
        old_len = len(queue_list)
        queue_list[:] = [(u, t, r) for u, t, r in queue_list if u != uid]
        if len(queue_list) < old_len:
            await callback_query.answer("✅ Queue se hata diya gaya!", show_alert=True)
        else:
            await callback_query.answer("⚠️ Cancelling task... Please wait.", show_alert=True)

@app.on_message(filters.command("cancel"))
async def cancel_cmd(client, message: Message):
    uid = message.from_user.id
    cancel_flags[uid] = True
    queue_list[:] = [(u, t, r) for u, t, r in queue_list if u != uid]
    await message.reply_text("⚠️ Cancel signal sent.")

async def progress_bar(current, total, action, message, start_time, edit_info, user_id):
    if cancel_flags.get(user_id): raise Exception("Cancelled by user!")
    now = time.time()
    if now - edit_info.get("last_edit", 0) > 3 or current == total:
        edit_info["last_edit"] = now
        percentage = current * 100 / total
        elapsed = now - start_time
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        if user_id in task_progress:
            p = task_progress[user_id]
            p["percent"] = percentage
            p["speed"] = f"{speed/(1024*1024):.2f} MB/s"
            p["processed"] = f"{current/(1024*1024):.1f} MB"
            p["total"] = f"{total/(1024*1024):.1f} MB"
            p["eta"] = format_time(eta)
            p["elapsed"] = format_time(elapsed)
            p["phase"] = action
        bar_length = 15
        completed = int((percentage / 100) * bar_length)
        bar = "■" * completed + "□" * (bar_length - completed)
        text = (f"**{action}**\n`[{bar}] {percentage:.1f}%`\n\n"
                f"**Processed:** {current/(1024*1024):.1f} MB / {total/(1024*1024):.1f} MB\n"
                f"**Speed:** {speed/(1024*1024):.2f} MB/s\n**ETA:** {format_time(eta)}")
        try: await message.edit_text(text, reply_markup=get_cancel_button(user_id))
        except: pass

async def get_video_metadata(video_path):
    try:
        cmd = f'ffprobe -v quiet -print_format json -show_format -show_streams "{video_path}"'
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        data = json.loads(stdout)
        w, h = 0, 0
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                w, h = int(stream.get('width', 0)), int(stream.get('height', 0))
                break
        duration = int(float(data.get('format', {}).get('duration', 0)))
        return w, h, duration
    except: return 0, 0, 0

# --- PREMIUM SETTINGS COMMANDS ---
def settings_keyboard(user_id):
    s = get_settings(user_id)
    codec_display = s["codec"].replace("lib", "")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎬 Mode: {s['mode'].upper()}", callback_data="settings_mode"),
         InlineKeyboardButton(f"💾 Codec: {codec_display}", callback_data="settings_codec"),
         InlineKeyboardButton(f"⚡ Preset: {s['preset'].upper()}", callback_data="settings_preset")],
        [InlineKeyboardButton(f"📺 360p CRF: {s['crf']['360p']}", callback_data="settings_crf_360p"),
         InlineKeyboardButton(f"📺 480p CRF: {s['crf']['480p']}", callback_data="settings_crf_480p"),
         InlineKeyboardButton(f"📺 720p CRF: {s['crf']['720p']}", callback_data="settings_crf_720p"),
         InlineKeyboardButton(f"📺 1080p CRF: {s['crf']['1080p']}", callback_data="settings_crf_1080p")],
        [InlineKeyboardButton(f"🔊 360p: {s['audio']['360p']}", callback_data="settings_audio_360p"),
         InlineKeyboardButton(f"🔊 480p: {s['audio']['480p']}", callback_data="settings_audio_480p"),
         InlineKeyboardButton(f"🔊 720p: {s['audio']['720p']}", callback_data="settings_audio_720p"),
         InlineKeyboardButton(f"🔊 1080p: {s['audio']['1080p']}", callback_data="settings_audio_1080p")],
        [InlineKeyboardButton("❌ Close", callback_data="settings_close")]
    ])

def settings_text(user_id):
    s = get_settings(user_id)
    codec_display = s["codec"].replace("lib", "")
    return (f"⚙️ **Your Premium Settings**\n\n"
            f"🎬 **Mode:** `{s['mode'].upper()}`\n"
            f"💾 **Codec:** `{codec_display}` | ⚡ **Preset:** `{s['preset'].upper()}`\n\n"
            f"📺 `360p` → CRF `{s['crf']['360p']}` | Audio `{s['audio']['360p']}`\n"
            f"📺 `480p` → CRF `{s['crf']['480p']}` | Audio `{s['audio']['480p']}`\n"
            f"📺 `720p` → CRF `{s['crf']['720p']}` | Audio `{s['audio']['720p']}`\n"
            f"📺 `1080p` → CRF `{s['crf']['1080p']}` | Audio `{s['audio']['1080p']}`")

def setting_value_buttons(setting_key, options, user_id, back_action="settings_main"):
    buttons = []
    current = get_setting_value(setting_key, user_id)
    for opt in options:
        label = opt
        if str(opt).lower() == str(current).lower():
            label = f"✅ {opt}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"set_{setting_key}_{opt}")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data=back_action)])
    return InlineKeyboardMarkup(buttons)

def get_setting_value(key, user_id):
    s = get_settings(user_id)
    parts = key.split("_")
    if parts[0] == "mode":
        return s["mode"]
    elif parts[0] == "codec":
        return s["codec"].replace("lib", "")
    elif parts[0] == "preset":
        return s["preset"]
    elif parts[0] == "crf" and len(parts) >= 2:
        return s["crf"][parts[1]]
    elif parts[0] == "audio" and len(parts) >= 2:
        return s["audio"][parts[1]]

@app.on_message(filters.command(["settings", "set_mode", "set_codec", "set_preset", "set_crf", "set_audio"]))
async def premium_settings_guard(client, message: Message):
    if not is_premium(message.from_user.id, message.chat.id):
        return await message.reply_text("⛔ **Access Denied:** Ye command sirf Premium Members ke liye hai.")
    
    user_id = message.from_user.id
    cmd = message.command[0]
    args = message.command
    settings = get_settings(user_id)

    if cmd == "settings":
        await message.reply_text(settings_text(user_id), reply_markup=settings_keyboard(user_id))
    
    elif cmd == "set_mode" and len(args) == 2:
        if args[1].lower() in ["all", "360p", "480p", "720p", "1080p"]:
            settings["mode"] = args[1].lower()
            settings["_template"] = False
            await message.reply_text(f"✅ Mode set to: **{args[1].upper()}**")
            
    elif cmd == "set_codec" and len(args) == 2:
        if args[1].lower() in ["x264", "x265"]:
            settings["codec"] = f"lib{args[1].lower()}"
            settings["_template"] = False
            await message.reply_text(f"✅ Codec set to: **{args[1].upper()}**")
            
    elif cmd == "set_preset" and len(args) == 2:
        settings["preset"] = args[1].lower()
        settings["_template"] = False
        await message.reply_text(f"✅ Preset set to: **{args[1].lower()}**")

    elif cmd == "set_crf":
        if len(args) == 3 and args[1].lower() in ["360p", "480p", "720p", "1080p"] and args[2].isdigit():
            settings["crf"][args[1].lower()] = int(args[2])
            settings["_template"] = False
            await message.reply_text(f"✅ {args[1].upper()} CRF set to: **{args[2]}**")
        else:
            await message.reply_text("❌ Sahi Format: `/set_crf 480p 19`")
            
    elif cmd == "set_audio":
        if len(args) == 3 and args[1].lower() in ["360p", "480p", "720p", "1080p"]:
            settings["audio"][args[1].lower()] = args[2]
            settings["_template"] = False
            await message.reply_text(f"✅ {args[1].upper()} Audio set to: **{args[2]}**")
        else:
            await message.reply_text("❌ Sahi Format: `/set_audio 480p 64k`")

@app.on_callback_query(filters.regex(r"^settings_"))
async def settings_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if not is_premium(user_id, callback_query.message.chat.id):
        return await callback_query.answer("⛔ Premium required!", show_alert=True)
    
    data = callback_query.data
    s = get_settings(user_id)

    if data == "settings_main":
        await callback_query.message.edit_text(settings_text(user_id), reply_markup=settings_keyboard(user_id))
        await callback_query.answer()

    elif data == "settings_mode":
        kb = setting_value_buttons("mode", ["all", "360p", "480p", "720p", "1080p"], user_id)
        await callback_query.message.edit_text("🎬 **Select Encoding Mode:**\n\n`all` → sab resolutions encode karega\n`360p` → sirf 360p encode karega", reply_markup=kb)
        await callback_query.answer()

    elif data == "settings_codec":
        kb = setting_value_buttons("codec", ["x264", "x265"], user_id)
        await callback_query.message.edit_text("💾 **Select Codec:**\n\n`x264` → fast, widely compatible\n`x265` → smaller file, slower encode", reply_markup=kb)
        await callback_query.answer()

    elif data == "settings_preset":
        kb = setting_value_buttons("preset", ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"], user_id)
        await callback_query.message.edit_text("⚡ **Select Preset:**\n\nFaster = bigger file, less CPU\nSlower = smaller file, more CPU", reply_markup=kb)
        await callback_query.answer()

    elif data.startswith("settings_crf_"):
        res = data.replace("settings_crf_", "")
        crf_range = list(range(18, 30))
        kb = setting_value_buttons(f"crf_{res}", crf_range, user_id)
        await callback_query.message.edit_text(f"📺 **Select CRF for {res.upper()}:**\n\nLower = better quality, larger file\nHigher = worse quality, smaller file", reply_markup=kb)
        await callback_query.answer()

    elif data.startswith("settings_audio_"):
        res = data.replace("settings_audio_", "")
        audio_opts = ["32k", "64k", "96k", "128k", "160k", "192k"]
        kb = setting_value_buttons(f"audio_{res}", audio_opts, user_id)
        await callback_query.message.edit_text(f"🔊 **Select Audio Bitrate for {res.upper()}:**", reply_markup=kb)
        await callback_query.answer()

    elif data == "settings_close":
        await callback_query.message.delete()
        await callback_query.answer()

@app.on_callback_query(filters.regex(r"^set_"))
async def set_value_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if not is_premium(user_id, callback_query.message.chat.id):
        return await callback_query.answer("⛔ Premium required!", show_alert=True)

    data = callback_query.data
    s = get_settings(user_id)

    if data.startswith("set_mode_"):
        s["mode"] = data.replace("set_mode_", "")
        s["_template"] = False
    elif data.startswith("set_codec_"):
        s["codec"] = f"lib{data.replace('set_codec_', '')}"
        s["_template"] = False
    elif data.startswith("set_preset_"):
        s["preset"] = data.replace("set_preset_", "")
        s["_template"] = False
    elif data.startswith("set_crf_"):
        rest = data.replace("set_crf_", "")
        res, val = rest.split("_", 1)
        if val.isdigit():
            s["crf"][res] = int(val)
            s["_template"] = False
    elif data.startswith("set_audio_"):
        rest = data.replace("set_audio_", "")
        res, val = rest.split("_", 1)
        s["audio"][res] = val
        s["_template"] = False

    await callback_query.answer("✅ Setting updated!")
    await callback_query.message.edit_text(settings_text(user_id), reply_markup=settings_keyboard(user_id))

# --- VIDEO/DOC RECEIVER (sirf last media store karega, process nahi karega) ---
@app.on_message((filters.video | filters.document) & ~filters.command(["encode", "ec"]))
async def store_media(client, message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    last_media[user_id] = message

# --- CORE LOGIC (/encode or /ec COMMAND) ---
ALL_RES = ["360p", "480p", "720p", "1080p"]

def parse_ff_resolutions(message, target_message):
    text = message.text or ""
    parts = text.split()
    ff_args = None
    for i, p in enumerate(parts):
        if p == "-ff" and i + 1 < len(parts):
            ff_args = parts[i + 1]
            break
    if ff_args:
        ff_args = ff_args.strip()
        if ff_args.lower() == "all":
            return list(ALL_RES)
        raw = [x.strip().lower() for x in ff_args.replace(",", " ").split() if x.strip()]
        out = []
        for r in raw:
            if not r.endswith("p"):
                r += "p"
            if r in ALL_RES:
                out.append(r)
        if out:
            return out
    cap = (target_message.caption or "").lower()
    m = re.search(r'q\.\s*(\d+)\s*p?', cap)
    if m:
        r = m.group(1) + "p"
        if r in ALL_RES:
            return [r]
    return None

@app.on_message(filters.command(["encode", "ec"]))
async def add_to_queue(client, message: Message):
    if not message.from_user:
        return
    if not is_premium(message.from_user.id, message.chat.id):
        return await message.reply_text("⛔ **Premium Required:** Video encode karne ke liye Premium Access hona chahiye. Admin se sampark karein.")

    uid = message.from_user.id

    total_active = len(queue_list)
    if total_active >= Config.MAX_BOT_TASKS:
        return await message.reply_text(f"⛌ Bot pe **{Config.MAX_BOT_TASKS}** videos ki limit hai. Queue full hai — kuch complete hone ke baad try karein.")
    if Config.MAX_USER_TASKS is not None:
        user_count = sum(1 for u, *_ in queue_list if u == uid)
        if user_count >= Config.MAX_USER_TASKS:
            return await message.reply_text(f"⛌ Aap ek baar mein sirf **{Config.MAX_USER_TASKS}** video(s) queue kar sakte hain.")

    target_message = None

    # Agar kisi message ko reply kiya gaya hai, to us message ko check karo
    if message.reply_to_message:
        rmsg = message.reply_to_message
        if rmsg.video or (rmsg.document and rmsg.document.file_name):
            target_message = rmsg
    # Agar reply nahi hai, to user ke last sent media ko check karo
    elif uid in last_media:
        target_message = last_media[uid]

    if not target_message:
        return await message.reply_text("❌ Pehle video bhejein, fir `/encode` ya `/ec` command dein (ya video ko reply karke command use karein).")

    # Subtitle file hai to save karo
    if target_message.document and target_message.document.file_name and target_message.document.file_name.endswith(".srt"):
        s = get_settings(uid)
        if s["srt"] and os.path.exists(s["srt"]): os.remove(s["srt"])
        s["srt"] = await target_message.download(file_name=f"sub_{uid}.srt")
        return await message.reply_text("✅ Subtitle Saved!")

    # Video document hai (bina .srt extension ke) to error do
    if target_message.document and not target_message.document.file_name.endswith(".srt"):
        return await message.reply_text("❌ Sirf video files ya .srt subtitle files hi encode ki ja sakti hain.")

    res_list = parse_ff_resolutions(message, target_message)
    if res_list is None:
        s = get_settings(uid)
        mode = s["mode"]
        res_list = list(ALL_RES) if mode == "all" else [mode]

    await video_queue.put((target_message, res_list))
    res_tag = " ".join(res_list)
    queue_list.append((uid, time.time(), res_tag))
    queue_pos = sum(1 for u, *_ in queue_list if u != uid) + 1
    await message.reply_text(f"📥 Queued (#{queue_pos}) | `{res_tag}`", reply_markup=get_cancel_button(uid))

    global worker_running
    if not worker_running:
        worker_running = True
        asyncio.create_task(video_worker(client))

async def video_worker(client):
    while not video_queue.empty():
        message, user_resolutions = await video_queue.get()
        user_id = message.from_user.id
        current_processing["user_id"] = user_id
        current_processing["start_time"] = time.time()
        settings = get_settings(user_id)
        cancel_flags[user_id] = False
        input_file = None
        
        filename = getattr(getattr(message, "video", None), "file_name", None) or getattr(getattr(message, "document", None), "file_name", None) or f"video_{user_id}"
        task_progress[user_id] = {
            "filename": filename, "phase": "⏳ Queued", "percent": 0,
            "speed": "0 B/s", "processed": "0 B", "total": "0 B",
            "eta": "N/A", "elapsed": "0s", "gid": str(int(time.time()))
        }
        
        try:
            status = await message.reply_text("⏳ Download Starting...", reply_markup=get_cancel_button(user_id))
            task_progress[user_id]["phase"] = "⬇️ Downloading"
            hyper_dl = HyperTGDownload(app, num_parts=Config.DOWNLOAD_PARTS)
            input_file = await hyper_dl.download_media(
                message,
                file_name=os.path.join(Config.DOWNLOAD_DIR, str(user_id), ""),
                progress=progress_bar,
                progress_args=("📥 Downloading Video", status, time.time(), {"last_edit": 0}, user_id)
            )
            if input_file is None:
                return
            if not os.path.exists(input_file) or os.path.getsize(input_file) < 1024:
                return await status.edit_text("❌ Download failed — file empty or corrupted.")
            
            orig_w, orig_h, total_duration = await get_video_metadata(input_file)
            if total_duration == 0:
                return await status.edit_text("❌ Downloaded file is not a valid video.")
            input_size = os.path.getsize(input_file) / (1024*1024)
            print(f"[INFO] Input: {input_file} | Size: {input_size:.1f}MB | Duration: {total_duration}s | {orig_w}x{orig_h}")
            resolutions = user_resolutions

            for res in resolutions:
                if cancel_flags.get(user_id): break
                task_progress[user_id]["phase"] = f"⚙️ Encoding {res}"
                try:
                    output = await encode_video(input_file, res, status, settings, user_id, total_duration)
                    if not output or not os.path.exists(output) or os.path.getsize(output) < 1024:
                        await message.reply_text(f"❌ {res} encoding failed — output file invalid or empty.")
                        continue
                    await status.edit_text(f"⏳ Verifying {res}...", reply_markup=get_cancel_button(user_id))
                    
                    w, h, duration = await get_video_metadata(output)
                    if duration == 0:
                        await message.reply_text(f"❌ {res} metadata read failed — skipping upload.")
                        os.remove(output)
                        continue
                    
                    original_caption = message.caption or os.path.basename(output)
                    
                    task_progress[user_id]["phase"] = f"⬆️ Uploading {res}"
                    await hyper_upload(
                        client, message.chat.id, output,
                        caption=f"**{res}** | PREMIUM Encode ✅\n📁 {original_caption}",
                        w=w, h=h, duration=duration,
                        progress=progress_bar,
                        progress_args=(f"📤 Uploading {res}", status, time.time(), {"last_edit": 0}, user_id)
                    )
                    os.remove(output)
                except Exception as e:
                    if "Cancelled" not in str(e): await message.reply_text(f"❌ {res} failed: {e}")

            if cancel_flags.get(user_id): await status.edit_text("❌ Process Cancelled by User.")
            else: await status.delete()

        except Exception as e:
            if "Cancelled" in str(e): await status.edit_text("❌ Process Cancelled.")
            else: await message.reply_text(f"❌ Error: {e}")
        finally:
            if input_file and os.path.exists(input_file): os.remove(input_file)
            current_processing["user_id"] = None
            current_processing["start_time"] = 0
            queue_list[:] = [(uid, t, r) for uid, t, r in queue_list if uid != user_id]
            if user_id in task_progress: del task_progress[user_id]
            video_queue.task_done()
    
    global worker_running
    worker_running = False


async def hyper_upload(client, chat_id, file_path, caption, w, h, duration, progress, progress_args):
    hyper_ul = HyperTGUpload(num_workers=6)
    input_file = await hyper_ul.save_file(client, file_path, progress=progress, progress_args=progress_args)
    if input_file is None:
        return None
    is_big = isinstance(input_file, raw.types.InputFileBig)
    if is_big:
        media = raw.types.InputMediaUploadedDocument(
            file=input_file, mime_type="video/mp4", attributes=[
                raw.types.DocumentAttributeVideo(
                    supports_streaming=True, w=w or 0, h=h or 0, duration=duration or 0
                ),
                raw.types.DocumentAttributeFilename(file_name=os.path.basename(file_path)),
            ],
        )
    else:
        media = raw.types.InputMediaUploadedDocument(
            file=input_file, mime_type="video/mp4", attributes=[
                raw.types.DocumentAttributeVideo(
                    supports_streaming=True, w=w or 0, h=h or 0, duration=duration or 0
                ),
                raw.types.DocumentAttributeFilename(file_name=os.path.basename(file_path)),
            ],
            md5_checksum=input_file.md5_checksum,
        )
    peer = await client.resolve_peer(chat_id)
    await client.invoke(
        raw.functions.messages.SendMedia(
            peer=peer, media=media, message=caption, random_id=client.rnd_id()
        )
    )
    return True

# --- FFMPEG ENGINE (LOW RAM + LIVE PROGRESS) ---
async def encode_video(input_file, res_key, status: Message, settings, user_id, total_duration):
    raw_name = os.path.splitext(os.path.basename(input_file))[0]
    import re as _re
    clean_name = _re.sub(r'_(360p|480p|720p|1080p|2160p|240p)$', '', raw_name, flags=_re.IGNORECASE)
    clean_name = _re.sub(r'^(360p|480p|720p|1080p|2160p|240p)_', '', clean_name, flags=_re.IGNORECASE)
    clean_name = _re.sub(r'_(360p|480p|720p|1080p|2160p|240p)_', '_', clean_name, flags=_re.IGNORECASE)
    use_template = settings.get("_template", True)
    ext = "mkv" if use_template else "mp4"
    output_file = f"{clean_name}_{res_key}_{user_id}.{ext}" 
    
    if use_template:
        cmd = Config.FFMPEG_CMDS[res_key].format(input=input_file, output=output_file)
    else:
        scales = {"360p": "scale=-2:360", "480p": "scale=-2:480", "720p": "scale=-2:720", "1080p": "scale=-2:1080"}
        scale = scales[res_key]
        
        sub_file = settings["srt"]
        if sub_file and os.path.exists(sub_file):
            sub_file_fixed = sub_file.replace('\\', '/')
            sub_cmd = f'-vf "{scale},subtitles={sub_file_fixed}"'
        else:
            sub_cmd = f'-vf "{scale}"'

        cmd = f'ffmpeg -y -i "{input_file}" {sub_cmd} -map 0:v -map 0:a -map_metadata 0 -map_chapters 0 -c:v {settings["codec"]} -preset {settings["preset"]} -crf {settings["crf"][res_key]} -threads 2 -max_muxing_queue_size 1024 -pix_fmt yuv420p -c:a aac -b:a {settings["audio"][res_key]} "{output_file}"'

    print(f"[FFMPEG] Starting encode: {output_file}")

    proc = await asyncio.create_subprocess_shell(cmd, stderr=asyncio.subprocess.PIPE)
    time_regex = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})\.\d{2}")
    last_edit = 0
    error_log = ""

    while True:
        chunk = await proc.stderr.read(2048)
        if not chunk: break 
        if cancel_flags.get(user_id):
            try: proc.kill()
            except: pass
            if os.path.exists(output_file): os.remove(output_file)
            raise Exception("Cancelled by user")

        chunk_str = chunk.decode('utf-8', errors='ignore')
        error_log += chunk_str 
        if len(error_log) > 1500: error_log = error_log[-1500:]

        matches = time_regex.findall(chunk_str)
        if matches and total_duration > 0:
            h, m, s = map(int, matches[-1]) 
            elapsed_sec = h * 3600 + m * 60 + s
            percentage = min(100, (elapsed_sec / total_duration) * 100)
            
            if time.time() - last_edit > 4: 
                last_edit = time.time()
                if user_id in task_progress:
                    task_progress[user_id]["percent"] = percentage
                    task_progress[user_id]["elapsed"] = format_time(elapsed_sec)
                bar = "■" * int(percentage/5) + "□" * (20 - int(percentage/5))
                text = (f"⚙️ **Encoding {res_key.upper()}**\n\n`[{bar}] {percentage:.1f}%`\n\n**Codec:** {settings['codec'].upper()} | **Preset:** {settings['preset'].upper()}")
                try: await status.edit_text(text, reply_markup=get_cancel_button(user_id))
                except: pass

    await proc.wait()
    
    if proc.returncode != 0:
        print(f"\n--- FFMPEG CRASH LOG FOR {res_key} ---")
        print(error_log)
        if os.path.exists(output_file): os.remove(output_file)
        raise Exception(f"FFmpeg failed (exit code {proc.returncode})!")
    
    if not os.path.exists(output_file) or os.path.getsize(output_file) < 1024:
        raise Exception(f"Output file empty or missing after encode!")
    
    out_size = os.path.getsize(output_file) / (1024*1024)
    print(f"[FFMPEG] {res_key} done — {out_size:.1f}MB")
    return output_file

@app.on_message(filters.command("queue"))
async def queue_status(client, message: Message):
    user_id = message.from_user.id
    lines = [f"📊 **Status**\n"]
    task_count = 0

    if current_processing["user_id"] == user_id and user_id in task_progress:
        p = task_progress[user_id]
        bar = leech_bar(p["percent"])
        lines.append(f"╭ 🎯 **Task** — `{p['filename']}`")
        lines.append(f"├ **Status:** {p['phase']}")
        lines.append(f"├ `{bar}` **{p['percent']:.1f}%**")
        lines.append(f"├ **Speed:** {p['speed']}")
        lines.append(f"├ **Size:** {p['processed']} / {p['total']}")
        lines.append(f"├ **ETA:** {p['eta']}")
        lines.append(f"├ **Elapsed:** {p['elapsed']}")
        lines.append(f"╰ **Cancel:** `/cancel`\n")
        task_count += 1
    elif current_processing["user_id"] == user_id:
        elapsed = format_time(int(time.time() - current_processing["start_time"]))
        lines.append(f"╭ 🎯 **Task** — Processing...")
        lines.append(f"├ **Status:** ⚙️ Encoding")
        lines.append(f"├ **Elapsed:** {elapsed}")
        lines.append(f"╰ **Cancel:** `/cancel`\n")
        task_count += 1

    queued_tasks = [(uid, t, r) for uid, t, r in queue_list if uid == user_id]
    if queued_tasks:
        lines.append(f"📦 **Queued — {len(queued_tasks)} video(s)**")
        for i, (uid, t, r) in enumerate(queued_tasks, 1):
            lines.append(f"├ **{i}.** `{r}` — `video_{uid}_{int(t)}`")
        lines.append("╰ Use `/cancel` to remove.\n")

    total_all = len(queue_list) + (1 if current_processing["user_id"] else 0)
    lines.append(f"━━━━━━━━━━━━")
    lines.append(f"📊 **Total:** {total_all}/{Config.MAX_BOT_TASKS}")

    if task_count == 0 and not queued_tasks:
        lines = [f"📊 **Status**\n\nAapki koi bhi video abhi queue mein nahi hai.\nVideo bhejkar `/encode` likhein."]

    await message.reply_text("\n".join(lines))

@app.on_message(filters.command(["restart"]) & filters.user(Config.ADMIN_ID))
async def restart_cmd(client, message: Message):
    await message.reply_text("🔄 Restarting bot... Code will auto-update from GitHub.")
    os.execl(sys.executable, sys.executable, "bot.py")

@app.on_message(filters.command("start"))
async def start_cmd(client, message: Message):
    await set_bot_menu()
    await message.reply_text("👋 Welcome to Pro Encode Bot!\n\nVideo bhejein saath me `/encode` ya `/ec` command use karein.")

if __name__ == "__main__":
    print("Bot is starting...")
    app.run()
