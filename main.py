import os
import re
import sys
import time
import uuid
import json
import random
import logging
import tempfile
import threading
import subprocess
import psutil
from pyrogram.enums import ParseMode
from io import BytesIO
from datetime import datetime, timezone, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote, urljoin
import aiohttp
import aiofiles
import asyncio
import requests
import isodate
import psutil
import pymongo
from pymongo import MongoClient, ASCENDING
from bson import ObjectId
from bson.binary import Binary
from dotenv import load_dotenv
from flask import Flask, request
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pyrogram import Client, filters, errors
from pyrogram.enums import ChatType, ChatMemberStatus, ParseMode
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ChatPermissions,
)
from pyrogram.errors import RPCError
from pytgcalls import PyTgCalls, idle
from pytgcalls.types import MediaStream
from pytgcalls import filters as fl
from pytgcalls.types import (
    ChatUpdate,
    UpdatedGroupCallParticipant,
    Update as TgUpdate,
)
from pytgcalls.types.stream import StreamEnded
from typing import Union
import urllib
from FrozenMusic.infra.concurrency.ci import deterministic_privilege_validator
from FrozenMusic.telegram_client.vector_transport import vector_transport_resolver
from FrozenMusic.infra.vector.yt_vector_orchestrator import yt_vector_orchestrator
from FrozenMusic.infra.vector.yt_backup_engine import yt_backup_engine
from FrozenMusic.infra.chrono.chrono_formatter import quantum_temporal_humanizer
from FrozenMusic.vector_text_tools import vectorized_unicode_boldifier
from FrozenMusic.telegram_client.startup_hooks import precheck_channels

load_dotenv()

# --- Configuration ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ASSISTANT_SESSION = os.environ.get("ASSISTANT_SESSION")
OWNER_ID = int(os.getenv("OWNER_ID", "5268762773"))
WELCOME_STICKER_ID = os.getenv("WELCOME_STICKER_ID", "CAACAgUAAxkBAAPXZo8U9yVvX4Vf6c4J_p1Z1l0lUq0AAmMAA5E1sVd9eY1E_Kk7PzQE")


# --- Logging and Error Handling ---
logging.getLogger("pyrogram").setLevel(logging.ERROR)
def _custom_exception_handler(loop, context):
    exc = context.get("exception")
    if isinstance(exc, (KeyError, ValueError)) and ("ID not found" in str(exc) or "Peer id invalid" in str(exc)):
        return  
    if isinstance(exc, AttributeError) and "has no attribute 'write'" in str(exc):
        return
    loop.default_exception_handler(context)

asyncio.get_event_loop().set_exception_handler(_custom_exception_handler)

session_name = os.environ.get("SESSION_NAME", "music_bot1")
bot = Client(session_name, bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)
assistant = Client("assistant_account", session_string=ASSISTANT_SESSION)
call_py = PyTgCalls(assistant)

ASSISTANT_USERNAME = None
ASSISTANT_CHAT_ID = None
API_ASSISTANT_USERNAME = os.getenv("API_ASSISTANT_USERNAME")
BACKUP_SEARCH_API_URL = os.getenv("BACKUP_SEARCH_API_URL")

# --- MongoDB Setup ---
mongo_uri = os.environ.get("MongoDB_url")
mongo_client = MongoClient(mongo_uri)
db = mongo_client["music_bot"]
broadcast_collection  = db["broadcast"]
playlists_collection = db["playlists"]
state_backup = db["state_backup"]
welcome_db = db["welcome_settings"]

chat_containers = {}
playback_tasks = {}  
bot_start_time = time.time()
COOLDOWN = 10
chat_last_command = {}
chat_pending_commands = {}
QUEUE_LIMIT = 20
MAX_DURATION_SECONDS = 7200  
LOCAL_VC_LIMIT = 10
playback_mode = {}
LOG_CHAT_ID = "@fssghjjs"

async def is_admin_or_owner(client: Client, chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status in [ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR]:
            return True
        return False
    except Exception as e:
        print(f"Error checking admin status: {e}")
        return False

# --- Core Bot Functions (from previous code) ---
async def process_pending_command(chat_id, delay):
    await asyncio.sleep(delay)
    if chat_id in chat_pending_commands:
        message, cooldown_reply = chat_pending_commands.pop(chat_id)
        if cooldown_reply:
            try:
                await cooldown_reply.delete()
            except RPCError:
                pass
        await play_handler(bot, message)

async def clean_chat_resources(chat_id: int):
    if chat_id in playback_tasks:
        try:
            playback_tasks[chat_id].cancel()
        except Exception:
            pass
        del playback_tasks[chat_id]
    if chat_id in chat_containers:
        for song in chat_containers[chat_id]:
            try:
                if song.get('file_path'):
                    os.remove(song['file_path'])
            except Exception as e:
                print(f"Error deleting file for chat {chat_id}: {e}")
        del chat_containers[chat_id]
    chat_last_command.pop(chat_id, None)
    chat_pending_commands.pop(chat_id, None)
    playback_mode.pop(chat_id, None)

async def safe_leave_call(chat_id: int):
    try:
        await call_py.leave_call(chat_id)
    except Exception as e:
        print(f"Error leaving the voice chat: {e}")

def iso8601_to_human_readable(iso_duration):
    try:
        duration = isodate.parse_duration(iso_duration)
        total_seconds = int(duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}:{minutes:02}:{seconds:02}"
        return f"{minutes}:{seconds:02}"
    except Exception as e:
        return "Unknown duration"

async def fetch_youtube_link(query):
    try:
        url = f"https://teenage-liz-frozzennbotss-61567ab4.koyeb.app/search?title={query}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if "playlist" in data:
                        return data
                    return (
                        data.get("link"),
                        data.get("title"),
                        data.get("duration"),
                        data.get("thumbnail")
                    )
                else:
                    raise Exception(f"API returned status code {response.status}")
    except Exception as e:
        raise Exception(f"Failed to fetch YouTube link: {str(e)}")

async def fetch_youtube_link_backup(query):
    if not BACKUP_SEARCH_API_URL:
        raise Exception("Backup Search API URL not configured")
    backup_url = (
        f"{BACKUP_SEARCH_API_URL.rstrip('/')}"
        f"/search?title={urllib.parse.quote(query)}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(backup_url, timeout=30) as resp:
                if resp.status != 200:
                    raise Exception(f"Backup API returned status {resp.status}")
                data = await resp.json()
                if "playlist" in data:
                    return data
                return (
                    data.get("link"),
                    data.get("title"),
                    data.get("duration"),
                    data.get("thumbnail")
                )
    except Exception as e:
        raise Exception(f"Backup Search API error: {e}")

BOT_NAME = os.environ.get("BOT_NAME", "Frozen Music")
BOT_LINK = os.environ.get("BOT_LINK", "https://t.me/vcmusiclubot")

async def is_assistant_in_chat(chat_id):
    try:
        member = await assistant.get_chat_member(chat_id, ASSISTANT_USERNAME)
        return member.status is not None
    except Exception as e:
        error_message = str(e)
        if "USER_BANNED" in error_message or "Banned" in error_message:
            return "banned"
        elif "USER_NOT_PARTICIPANT" in error_message or "Chat not found" in error_message:
            return False
        print(f"Error checking assistant in chat: {e}")
        return False

def to_bold_unicode(text: str) -> str:
    bold_text = ""
    for char in text:
        if 'A' <= char <= 'Z':
            bold_text += chr(ord('𝗔') + (ord(char) - ord('A')))
        elif 'a' <= char <= 'z':
            bold_text += chr(ord('𝗮') + (ord(char) - ord('a')))
        else:
            bold_text += char
    return bold_text

@bot.on_message(filters.new_chat_members)
async def welcome_handler(_, message: Message):
    for member in message.new_chat_members:
        if member.is_bot:
            continue

        chat_id = message.chat.id
        chat_name = message.chat.title
        user_name = member.first_name

        welcome_settings = welcome_db.find_one({"chat_id": chat_id})
        custom_message = welcome_settings.get("message") if welcome_settings else None
        custom_sticker = welcome_settings.get("sticker_id") if welcome_settings else None

        if custom_message:
            welcome_text = custom_message.format(name=user_name, group_name=chat_name)
        else:
            welcome_text = (
                f"🎉 Welcome, **{user_name}** to **{chat_name}**!\n"
                f"We hope you enjoy your time here. Feel free to use my music commands like `/play`."
            )

        try:
            sticker_id = custom_sticker or WELCOME_STICKER_ID
            await bot.send_sticker(chat_id, sticker=sticker_id)
        except RPCError:
            print("Failed to send welcome sticker, falling back to text.")
            
        await bot.send_message(chat_id, welcome_text, parse_mode=ParseMode.MARKDOWN)

@bot.on_message(filters.command("start"))
async def start_handler(_, message: Message):
    user_id = message.from_user.id
    raw_name = message.from_user.first_name or ""
    styled_name = to_bold_unicode(raw_name)
    user_link = f"[{styled_name}](tg://user?id={user_id})"

    add_me_text = to_bold_unicode("Add Me")
    updates_text = to_bold_unicode("Updates")
    support_text = to_bold_unicode("Support")
    help_text = to_bold_unicode("Help")

    caption = (
        f"👋 нєу {user_link} 💠, 🥀\n\n"
        f"🎶 𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 『 {BOT_NAME.upper()} 』 🎵\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🚀 𝐓𝐨𝐩-𝐍𝐨𝐭𝐜𝐡 24×7 𝐔𝐩𝐭𝐢𝐦𝐞 ⚡\n"
        "🔊 𝐂𝐫𝐲𝐬𝐭𝐚𝐥-𝐂𝐥𝐞𝐚𝐫 𝐀𝐮𝐝𝐢𝐨 🎼\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🎧 𝐒𝐮𝐩𝐩𝐨𝐫𝐭𝐞𝐝 𝐏𝐥𝐚𝐭𝐟𝐨𝐫𝐦𝐬:\n"
        "   • YouTube 🎥\n"
        "   • Spotify 🎵\n"
        "   • Resso 🎶\n"
        "   • Apple Music 🍎\n"
        "   • SoundCloud ☁️\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "✨ 𝐒𝐦𝐚𝐫𝐭 𝐀𝐮𝐭𝐨-𝐒𝐮𝐠𝐠𝐞𝐬𝐭𝐢𝐨𝐧𝐬 𝙬𝙝𝙚𝙣 𝐪𝐮𝐞𝐮𝐞 𝐞𝐧𝐝𝐬 🔄\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🛠️ 𝐀𝐝𝐦𝐢𝐧 𝐂𝐨𝐦𝐦𝐚𝐧𝐝𝐬:\n"
        "   ▸ Pause ⏸️ | Resume ▶️ | Skip ⏭️\n"
        "   ▸ Stop ⏹️ | Mute 🔇 | Unmute 🔊\n"
        "   ▸ Tmute ⏲️ | Kick 👢 | Ban 🚫 | Unban ✅\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "❤️ 𝐂𝐨𝐮𝐩𝐥𝐞 𝐒𝐮𝐠𝐠𝐞𝐬𝐭𝐢𝐨𝐧 💕\n"
        "   (ʀᴀɴᴅᴏᴍ ᴘᴀɪʀ ɪɴ ɢʀᴏᴜᴘ)\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"๏ ᴄʟɪᴄᴋ {help_text} ʙᴇʟᴏᴡ ғᴏʀ ғᴜʟʟ ᴄᴏᴍᴍᴀɴᴅ ʟɪsᴛ 📜"
    )

    buttons = [
        [
            InlineKeyboardButton(f"➕ {add_me_text}", url=f"{BOT_LINK}?startgroup=true"),
            InlineKeyboardButton(f"📢 {updates_text}", url="https://t.me/bestshayri_raj")
        ],
        [
            InlineKeyboardButton(f"💬 {support_text}", url="https://t.me/+34oz1KeknQtlYTdl"),
            InlineKeyboardButton(f"❓ {help_text}", callback_data="show_help")
        ],
        [
            InlineKeyboardButton("👑 Owner", url="https://t.me/teamrajweb"),
            InlineKeyboardButton("⚡ Earning Zone", url="https://t.me/RAJCOMMITMENTBOT")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)

    await message.reply_animation(
        animation="https://filehosting.kustbotsweb.workers.dev/tfq.mp4",
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )

    if message.chat.type == ChatType.PRIVATE:
        if not broadcast_collection.find_one({"chat_id": user_id}):
            broadcast_collection.insert_one({"chat_id": user_id, "type": "private"})
    elif message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if not broadcast_collection.find_one({"chat_id": message.chat.id}):
            broadcast_collection.insert_one({"chat_id": message.chat.id, "type": "group"})

@bot.on_callback_query(filters.regex("^go_back$"))
async def go_back_callback(_, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    raw_name = callback_query.from_user.first_name or ""
    styled_name = to_bold_unicode(raw_name)
    user_link = f"[{styled_name}](tg://user?id={user_id})"
    add_me_text = to_bold_unicode("Add Me")
    updates_text = to_bold_unicode("Updates")
    support_text = to_bold_unicode("Support")
    help_text = to_bold_unicode("Help")
    caption = (
        f"👋 нєу {user_link} 💠, 🥀\n\n"
        f"🎶 𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 『 {BOT_NAME.upper()} 』 🎵\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🚀 𝐓𝐨𝐩-𝐍𝐨𝐭𝐜𝐡 24×7 𝐔p𝐭𝐢𝐦𝐞 ⚡\n"
        "🔊 𝐂𝐫𝐲𝐬𝐭𝐚𝐥-𝐂𝐥𝐞𝐚𝐫 𝐀𝐮𝐝𝐢𝐨 🎼\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🎧 𝐒𝐮𝐩𝐩𝐨𝐫𝐭𝐞𝐝 𝐏𝐥𝐚𝐭𝐟𝐨𝐫𝐦𝐬:\n"
        "   • YouTube 🎥\n"
        "   • Spotify 🎵\n"
        "   • Resso 🎶\n"
        "   • Apple Music 🍎\n"
        "   • SoundCloud ☁️\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "✨ 𝐒𝐦𝐚𝐫𝐭 𝐀𝐮𝐭𝐨-𝐒𝐮𝐠𝐠𝐞𝐬𝐭𝐢𝐨𝐧𝐬 𝙬𝙝𝙚𝙣 𝐪𝐮𝐞𝐮𝐞 𝐞𝐧𝐝𝐬 🔄\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🛠️ 𝐀𝐝𝐦𝐢𝐧 𝐂𝐨𝐦𝐦𝐚𝐧𝐝𝐬:\n"
        "   ▸ Pause ⏸️ | Resume ▶️ | Skip ⏭️\n"
        "   ▸ Stop ⏹️ | Mute 🔇 | Unmute 🔊\n"
        "   ▸ Tmute ⏲️ | Kick 👢 | Ban 🚫 | Unban ✅\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "❤️ 𝐂𝐨𝐮𝐩𝐥𝐞 𝐒𝐮𝐠𝐠𝐞𝐬𝐭𝐢𝐨𝐧 💕\n"
        "   (ʀᴀɴᴅᴏᴍ ᴘᴀɪʀ ɪɴ ɢʀᴏᴜᴘ)\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"๏ ᴄʟɪᴄᴋ {help_text} ʙᴇʟᴏᴡ ғᴏʀ ғᴜʟʟ ᴄᴏᴍᴍᴀɴᴅ ʟɪsᴛ 📜"
    )
    buttons = [
        [
            InlineKeyboardButton(f"➕ {add_me_text}", url=f"{BOT_LINK}?startgroup=true"),
            InlineKeyboardButton(f"📢 {updates_text}", url="https://t.me/bestshayri_raj")
        ],
        [
            InlineKeyboardButton(f"💬 {support_text}", url="https://t.me/+34oz1KeknQtlYTdl"),
            InlineKeyboardButton(f"❓ {help_text}", callback_data="show_help")
        ],
        [
            InlineKeyboardButton("👑 Owner", url="https://t.me/teamrajweb"),
            InlineKeyboardButton("⚡ Earning Zone", url="https://t.me/RAJCOMMITMENTBOT")
        ]
    ]
    await callback_query.message.edit_caption(
        caption=caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@bot.on_callback_query(filters.regex("^show_help$"))
async def show_help_callback(_, callback_query: CallbackQuery):
    help_text = ">📜 *Choose a category to explore commands:*"
    buttons = [
        [
            InlineKeyboardButton("🎵 Music Controls", callback_data="help_music"),
            InlineKeyboardButton("🛡️ Admin Tools", callback_data="help_admin")
        ],
        [
            InlineKeyboardButton("❤️ Couple Suggestion", callback_data="help_couple"),
            InlineKeyboardButton("🔍 Utility", callback_data="help_util")
        ],
        [
            InlineKeyboardButton("🏠 Home", callback_data="go_back")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

@bot.on_callback_query(filters.regex("^help_music$"))
async def help_music_callback(_, callback_query: CallbackQuery):
    text = (
        ">🎵 *Music & Playback Commands*\n\n"
        ">➜ `/play <song name or URL>`\n"
        "   • Play a song (YouTube/Spotify/Resso/Apple Music/SoundCloud).\n"
        "   • If replied to an audio/video, plays it directly.\n\n"
        ">➜ `/queue`\n"
        "   • View the current queue and jump to a song.\n\n"
        ">➜ `/saveplaylist <name>`\n"
        "   • Save the current queue to your personal playlist.\n\n"
        ">➜ `/loadplaylist <name>`\n"
        "   • Load and play a saved playlist.\n\n"
        ">➜ `/myplaylists`\n"
        "   • View your saved playlists.\n\n"
        ">➜ `/skip`\n"
        "   • Skip the currently playing song. (Admins only)\n\n"
        ">➜ `/pause`\n"
        "   • Pause the current stream. (Admins only)\n\n"
        ">➜ `/resume`\n"
        "   • Resume a paused stream. (Admins only)\n\n"
        ">➜ `/stop` or `/end`\n"
        "   • Stop playback and clear the queue. (Admins only)"
    )
    buttons = [[InlineKeyboardButton("🔙 Back", callback_data="show_help")]]
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_callback_query(filters.regex("^help_admin$"))
async def help_admin_callback(_, callback_query: CallbackQuery):
    text = (
        "🛡️ *Admin & Moderation Commands*\n\n"
        ">➜ `/mute`\n"
        "   • Mute a user indefinitely.\n\n"
        ">➜ `/unmute`\n"
        "   • Unmute a previously muted user.\n\n"
        ">➜ `/tmute <time>`\n"
        "   • Temporarily mute for a set duration. (e.g. `10m`, `2h`).\n\n"
        ">➜ `/kick`\n"
        "   • Kick (ban + unban) a user immediately.\n\n"
        ">➜ `/ban`\n"
        "   • Ban a user.\n\n"
        ">➜ `/unban`\n"
        "   • Unban a previously banned user.\n\n"
        ">➜ `/welcomeset <text>`\n"
        "   • Set a custom welcome message for new users. Use `{name}` and `{group_name}`.\n\n"
        ">➜ `/welcomesticker`\n"
        "   • Reply to a sticker to set it as the welcome sticker."
    )
    buttons = [[InlineKeyboardButton("🔙 Back", callback_data="show_help")]]
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


@bot.on_callback_query(filters.regex("^help_couple$"))
async def help_couple_callback(_, callback_query: CallbackQuery):
    text = (
        "❤️ *Couple Suggestion Command*\n\n"
        ">➜ `/couple`\n"
        "   • Picks two random non-bot members and posts a “couple” image with their names.\n"
        "   • Caches daily so the same pair appears until midnight UTC.\n"
        "   • Uses per-group member cache for speed."
    )
    buttons = [[InlineKeyboardButton("🔙 Back", callback_data="show_help")]]
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


@bot.on_callback_query(filters.regex("^help_util$"))
async def help_util_callback(_, callback_query: CallbackQuery):
    text = (
        "🔍 *Utility & Extra Commands*\n\n"
        ">➜ `/ping`\n"
        "   • Check bot’s response time and uptime.\n\n"
        ">➜ `/clear`\n"
        "   • Clear the entire queue. (Admins only)\n\n"
        ">➜ Auto-Suggestions:\n"
        "   • When the queue ends, the bot automatically suggests new songs via inline buttons.\n\n"
        ">➜ *Audio Quality & Limits*\n"
        "   • Streams up to 2 hours 10 minutes, but auto-fallback for longer. (See `MAX_DURATION_SECONDS`)\n"
    )
    buttons = [[InlineKeyboardButton("🔙 Back", callback_data="show_help")]]
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_message(filters.group & filters.command("mute"))
async def mute_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to mute them.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.restrict_chat_member(
            message.chat.id,
            target_user_id,
            ChatPermissions()
        )
        await message.reply(f"🔇 **{target_user_name}** has been muted.")
    except Exception as e:
        await message.reply(f"❌ Failed to mute user: {e}")

@bot.on_message(filters.group & filters.command("tmute"))
async def tmute_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to mute them.")

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        return await message.reply("❌ Usage: `/tmute <time>`. Example: `/tmute 10m`.")

    time_str = parts[1]
    
    # Simple time parsing (e.g., 10m, 2h, 1d)
    match = re.match(r"(\d+)([mhd])", time_str)
    if not match:
        return await message.reply("❌ Invalid time format. Use `m` for minutes, `h` for hours, `d` for days.")
        
    value, unit = int(match.group(1)), match.group(2)
    if unit == 'm':
        mute_until = datetime.now() + timedelta(minutes=value)
    elif unit == 'h':
        mute_until = datetime.now() + timedelta(hours=value)
    elif unit == 'd':
        mute_until = datetime.now() + timedelta(days=value)
    else:
        return await message.reply("❌ Invalid time unit. Use `m`, `h`, or `d`.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.restrict_chat_member(
            message.chat.id,
            target_user_id,
            ChatPermissions(can_send_messages=False),
            until_date=mute_until
        )
        await message.reply(f"🔇 **{target_user_name}** has been muted for {value}{unit}.")
    except Exception as e:
        await message.reply(f"❌ Failed to mute user: {e}")


@bot.on_message(filters.group & filters.command("unmute"))
async def unmute_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to unmute them.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.restrict_chat_member(
            message.chat.id,
            target_user_id,
            ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        )
        await message.reply(f"🔊 **{target_user_name}** has been unmuted.")
    except Exception as e:
        await message.reply(f"❌ Failed to unmute user: {e}")

@bot.on_message(filters.group & filters.command("ban"))
async def ban_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to ban them.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.ban_chat_member(message.chat.id, target_user_id)
        await message.reply(f"🚫 **{target_user_name}** has been banned.")
    except Exception as e:
        await message.reply(f"❌ Failed to ban user: {e}")

@bot.on_message(filters.group & filters.command("unban"))
async def unban_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to unban them.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.unban_chat_member(message.chat.id, target_user_id)
        await message.reply(f"✅ **{target_user_name}** has been unbanned.")
    except Exception as e:
        await message.reply(f"❌ Failed to unban user: {e}")

@bot.on_message(filters.group & filters.command("kick"))
async def kick_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to kick them.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.ban_chat_member(message.chat.id, target_user_id)
        await bot.unban_chat_member(message.chat.id, target_user_id)
        await message.reply(f"👢 **{target_user_name}** has been kicked from the group.")
    except Exception as e:
        await message.reply(f"❌ Failed to kick user: {e}")

@bot.on_message(filters.group & filters.command("welcomeset"))
async def welcomeset_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return await message.reply("❌ Please provide a custom welcome message.\nExample: `/welcomeset Welcome {name} to {group_name}!`")

    custom_message = text[1].strip()
    chat_id = message.chat.id

    welcome_db.update_one(
        {"chat_id": chat_id},
        {"$set": {"message": custom_message}},
        upsert=True
    )
    await message.reply("✅ Welcome message has been updated!")

@bot.on_message(filters.group & filters.command("welcomesticker"))
async def welcomesticker_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message or not message.reply_to_message.sticker:
        return await message.reply("❌ Reply to a sticker to set it as the welcome sticker.")

    sticker_id = message.reply_to_message.sticker.file_id
    chat_id = message.chat.id

    welcome_db.update_one(
        {"chat_id": chat_id},
        {"$set": {"sticker_id": sticker_id}},
        upsert=True
    )
    await message.reply("✅ Welcome sticker has been updated!")


@bot.on_message(filters.group & filters.regex(r'^/play(?:@\w+)?(?:\s+(?P<query>.+))?$'))
async def play_handler(_, message: Message):
    # (Existing play_handler code)
    chat_id = message.chat.id

    if message.reply_to_message and (message.reply_to_message.audio or message.reply_to_message.video):
        processing_message = await message.reply("❄️")
        orig = message.reply_to_message
        media = orig.video or orig.audio
        if getattr(media, 'file_size', 0) > 100 * 1024 * 1024:
            await processing_message.edit("❌ Audio file too large. Maximum allowed size is 100MB.")
            return
        await processing_message.edit("⏳ Please wait, downloading audio…")
        try:
            file_path = await bot.download_media(media)
        except Exception as e:
            await processing_message.edit(f"❌ Failed to download media: {e}")
            return
        thumb_path = None
        try:
            thumbs = media.thumbs
            if thumbs:
                thumb_path = await bot.download_media(thumbs[0])
        except Exception:
            pass
        duration = media.duration or 0
        title = getattr(media, 'file_name', 'Untitled')
        song_info = {
            'url': file_path,
            'title': title,
            'duration': format_time(duration),
            'duration_seconds': duration,
            'requester': message.from_user.first_name,
            'thumbnail': thumb_path,
            'is_local': True
        }
        chat_containers.setdefault(chat_id, []).append(song_info)
        if len(chat_containers[chat_id]) == 1:
            await fallback_local_playback(chat_id, processing_message, song_info)
        else:
            await processing_message.edit(f"✨ Added `{song_info['title']}` to the queue at position {len(chat_containers[chat_id])}")
        return

    match = message.matches[0]
    query = (match.group('query') or "").strip()
    try:
        await message.delete()
    except RPCError:
        pass
    now_ts = time.time()
    if chat_id in chat_last_command and (now_ts - chat_last_command[chat_id]) < COOLDOWN:
        remaining = int(COOLDOWN - (now_ts - chat_last_command[chat_id]))
        if chat_id in chat_pending_commands:
            await bot.send_message(chat_id, f"⏳ A command is already queued for this chat. Please wait {remaining}s.")
        else:
            cooldown_reply = await bot.send_message(chat_id, f"⏳ On cooldown. Processing in {remaining}s.")
            chat_pending_commands[chat_id] = (message, cooldown_reply)
            asyncio.create_task(process_pending_command(chat_id, remaining))
        return
    chat_last_command[chat_id] = now_ts
    if not query:
        await bot.send_message(
            chat_id,
            "❌ You did not specify a song.\n\n"
            "Correct usage: /play <song name>\nExample: /play shape of you"
        )
        return
    await process_play_command(message, query)


async def process_play_command(message: Message, query: str):
    chat_id = message.chat.id
    processing_message = await message.reply("❄️")
    status = await is_assistant_in_chat(chat_id)
    if status == "banned":
        await processing_message.edit("❌ Assistant is banned from this chat.")
        return
    if status is False:
        invite_link = await bot.export_chat_invite_link(chat_id)
        if not invite_link:
            await processing_message.edit("❌ Could not obtain an invite link to add the assistant.")
            return
        try:
            await assistant.join_chat(invite_link)
            await processing_message.edit("✅ Assistant has joined the chat.")
        except Exception as e:
            await processing_message.edit(f"❌ Failed to invite assistant: {e}")
            return
    if "youtu.be" in query:
        m = re.search(r"youtu\.be/([^?&]+)", query)
        if m:
            query = f"https://www.youtube.com/watch?v={m.group(1)}"
    try:
        result = await fetch_youtube_link(query)
    except Exception as primary_err:
        await processing_message.edit("⚠️ Primary search failed. Using backup API, this may take a few seconds…")
        try:
            result = await fetch_youtube_link_backup(query)
        except Exception as backup_err:
            await processing_message.edit(f"❌ Both search APIs failed:\nPrimary: {primary_err}\nBackup: {backup_err}")
            return
    if isinstance(result, dict) and "playlist" in result:
        playlist_items = result["playlist"]
        if not playlist_items:
            await processing_message.edit("❌ No videos found in the playlist.")
            return
        chat_containers.setdefault(chat_id, [])
        for item in playlist_items:
            secs = isodate.parse_duration(item["duration"]).total_seconds()
            if secs > MAX_DURATION_SECONDS:
                continue
            chat_containers[chat_id].append({
                "url": item["link"],
                "title": item["title"],
                "duration": iso8601_to_human_readable(item["duration"]),
                "duration_seconds": secs,
                "requester": message.from_user.first_name,
                "thumbnail": item["thumbnail"],
                'is_local': False
            })
        total = len(playlist_items)
        first_song_title = chat_containers[chat_id][-total]['title']
        reply_text = (
            f"✨ Added to queue: {total} songs from playlist.\n"
            f"**First song:** {first_song_title}"
        )
        await message.reply(reply_text)
        if len(chat_containers[chat_id]) == total:
            first_song_info = chat_containers[chat_id][0]
            await fallback_local_playback(chat_id, processing_message, first_song_info)
        else:
            await processing_message.delete()
    else:
        video_url, title, duration_iso, thumb = result
        if not video_url:
            await processing_message.edit("❌ Could not find the song. Try another query.\nSupport: @frozensupport1")
            return
        secs = isodate.parse_duration(duration_iso).total_seconds()
        if secs > MAX_DURATION_SECONDS:
            await processing_message.edit("❌ Streams longer than 2 hours are not allowed.")
            return
        readable = iso8601_to_human_readable(duration_iso)
        chat_containers.setdefault(chat_id, [])
        song_info = {
            "url": video_url,
            "title": title,
            "duration": readable,
            "duration_seconds": secs,
            "requester": message.from_user.first_name,
            "thumbnail": thumb,
            'is_local': False
        }
        chat_containers[chat_id].append(song_info)
        if len(chat_containers[chat_id]) == 1:
            await fallback_local_playback(chat_id, processing_message, song_info)
        else:
            queue_buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Skip", callback_data="skip"), InlineKeyboardButton("🗑 Clear", callback_data="clear")]
            ])
            await message.reply(
                f"✨ Added to queue :\n\n"
                f"**❍ Title ➥** {title}\n"
                f"**❍ Time ➥** {readable}\n"
                f"**❍ By ➥ ** {message.from_user.first_name}\n"
                f"**Queue number:** {len(chat_containers[chat_id])}",
                reply_markup=queue_buttons
            )
            await processing_message.delete()


@bot.on_message(filters.group & filters.command("queue"))
async def queue_handler(_, message: Message):
    chat_id = message.chat.id
    if chat_id not in chat_containers or not chat_containers[chat_id]:
        await message.reply("❌ The queue is empty.")
        return
    current_song = chat_containers[chat_id][0]
    queue_list = "\n".join([f"**{i+1}.** `{_one_line_title(song['title'])}`" for i, song in enumerate(chat_containers[chat_id])])
    text = (
        f"**🎧 Now Playing:**\n"
        f"**»** `{current_song['title']}`\n\n"
        f"**🎶 Queue ({len(chat_containers[chat_id])} songs):**\n"
        f"{queue_list}"
    )
    await message.reply(text)

@bot.on_message(filters.group & filters.command("saveplaylist"))
async def save_playlist_handler(_, message: Message):
    if not message.reply_to_message:
        await message.reply("❌ You must reply to a song to save a playlist.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("❌ Please provide a name for your playlist. Usage: `/saveplaylist <name>`")
        return
    playlist_name = parts[1].strip()
    user_id = message.from_user.id
    if user_id not in chat_containers or not chat_containers[user_id]:
        await message.reply("❌ You do not have an active queue to save.")
        return
    playlist_data = chat_containers[user_id]
    try:
        playlists_collection.update_one(
            {"user_id": user_id, "name": playlist_name},
            {"$set": {"songs": playlist_data}},
            upsert=True
        )
        await message.reply(f"✅ Playlist `{playlist_name}` saved with {len(playlist_data)} songs.")
    except Exception as e:
        await message.reply(f"❌ Failed to save playlist: {e}")

@bot.on_message(filters.group & filters.command("loadplaylist"))
async def load_playlist_handler(_, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("❌ Please provide the name of the playlist to load. Usage: `/loadplaylist <name>`")
        return
    playlist_name = parts[1].strip()
    user_id = message.from_user.id
    playlist_doc = playlists_collection.find_one({"user_id": user_id, "name": playlist_name})
    if not playlist_doc:
        await message.reply(f"❌ Playlist `{playlist_name}` not found.")
        return
    songs = playlist_doc.get("songs", [])
    if not songs:
        await message.reply(f"❌ Playlist `{playlist_name}` is empty.")
        return
    chat_id = message.chat.id
    chat_containers[chat_id] = songs
    await message.reply(f"✅ Loaded playlist `{playlist_name}` with {len(songs)} songs.\nPlaying the first song now...")
    first_song_info = chat_containers[chat_id][0]
    dummy_msg = await bot.send_message(chat_id, f"🎧 Preparing first song from playlist: **{first_song_info['title']}** ...")
    await fallback_local_playback(chat_id, dummy_msg, first_song_info)

@bot.on_message(filters.group & filters.command("myplaylists"))
async def my_playlists_handler(_, message: Message):
    user_id = message.from_user.id
    playlists = list(playlists_collection.find({"user_id": user_id}))
    if not playlists:
        await message.reply("❌ You have no saved playlists.")
        return
    playlist_list = "\n".join([f"**»** `{p['name']}` ({len(p.get('songs', []))} songs)" for p in playlists])
    await message.reply(f"**Your Saved Playlists:**\n\n{playlist_list}")

def _one_line_title(full_title: str) -> str:
    MAX_TITLE_LEN = 20
    if len(full_title) <= MAX_TITLE_LEN:
        return full_title
    else:
        return full_title[:(MAX_TITLE_LEN - 1)] + "…"

def format_time(seconds: float) -> str:
    secs = int(seconds)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    else:
        return f"{m}:{s:02d}"

def get_progress_bar_styled(elapsed: float, total: float, bar_length: int = 14) -> str:
    if total <= 0:
        return "Progress: N/A"
    fraction = min(elapsed / total, 1)
    marker_index = int(fraction * bar_length)
    if marker_index >= bar_length:
        marker_index = bar_length - 1
    left = "━" * marker_index
    right = "─" * (bar_length - marker_index - 1)
    bar = left + "❄️" + right
    return f"{format_time(elapsed)} {bar} {format_time(total)}"

async def update_progress_caption(
    chat_id: int,
    progress_message: Message,
    start_time: float,
    total_duration: float,
    base_caption: str
):
    while True:
        elapsed = time.time() - start_time
        if elapsed > total_duration:
            elapsed = total_duration
        progress_bar = get_progress_bar_styled(elapsed, total_duration)
        control_row = [
            InlineKeyboardButton(text="▷", callback_data="pause"),
            InlineKeyboardButton(text="II", callback_data="resume"),
            InlineKeyboardButton(text="‣‣I", callback_data="skip"),
            InlineKeyboardButton(text="▢", callback_data="stop")
        ]
        progress_button = InlineKeyboardButton(text=progress_bar, callback_data="progress")
        playlist_button = InlineKeyboardButton(text="➕ᴀᴅᴅ тσ ρℓαυℓιѕт➕", callback_data="add_to_playlist")
        new_keyboard = InlineKeyboardMarkup([
            control_row,
            [progress_button],
            [playlist_button]
        ])
        try:
            await bot.edit_message_caption(
                chat_id,
                progress_message.id,
                caption=base_caption,
                reply_markup=new_keyboard,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                print(f"Error updating progress caption for chat {chat_id}: {e}")
            break
        if elapsed >= total_duration:
            break
        await asyncio.sleep(18)

async def fallback_local_playback(chat_id: int, message: Message, song_info: dict):
    playback_mode[chat_id] = "local"
    try:
        if chat_id in playback_tasks:
            playback_tasks[chat_id].cancel()
        try:
            await message.edit(f"Starting local playback for ⚡ {song_info['title']}...")
        except Exception:
            message = await bot.send_message(
                chat_id,
                f"Starting local playback for ⚡ {song_info['title']}..."
            )
        video_url = song_info.get("url")
        if not video_url:
            await message.edit("❌ Invalid song URL.")
            chat_containers[chat_id].pop(0)
            return
        media_path = await vector_transport_resolver(video_url)
        song_info['file_path'] = media_path
        await call_py.play(
            chat_id,
            MediaStream(media_path, video_flags=MediaStream.Flags.IGNORE)
        )
        playback_tasks[chat_id] = asyncio.current_task()
        total_duration = song_info.get("duration_seconds", 0)
        one_line = _one_line_title(song_info["title"])
        base_caption = (
            "<blockquote>"
            "<b> @bestshayri_raj  🎧 Streaming</b> (Local Playback)\n\n"
            f"❍ <b>Title:</b> {one_line}\n"
            f"❍ <b>Requested by:</b> {song_info['requester']}"
            "</blockquote>"
        )
        initial_progress = get_progress_bar_styled(0, total_duration)
        control_row = [
            InlineKeyboardButton(text="▷", callback_data="pause"),
            InlineKeyboardButton(text="II", callback_data="resume"),
            InlineKeyboardButton(text="‣‣I", callback_data="skip"),
            InlineKeyboardButton(text="▢", callback_data="stop"),
        ]
        progress_button = InlineKeyboardButton(text=initial_progress, callback_data="progress")
        playlist_button = InlineKeyboardButton(text="➕ᴀᴅᴅ тσ ρℓαυℓιѕт➕", callback_data="add_to_playlist")
        base_keyboard = InlineKeyboardMarkup([control_row, [progress_button], [playlist_button]])
        thumb_url = song_info.get("thumbnail")
        if thumb_url:
            progress_message = await message.reply_photo(
                photo=thumb_url,
                caption=base_caption,
                reply_markup=base_keyboard,
                parse_mode=ParseMode.HTML
            )
        else:
            progress_message = await message.reply(
                caption=base_caption,
                reply_markup=base_keyboard,
                parse_mode=ParseMode.HTML
            )
        try:
            await message.delete()
        except RPCError:
            pass
        asyncio.create_task(
            update_progress_caption(
                chat_id,
                progress_message,
                time.time(),
                total_duration,
                base_caption
            )
        )
        asyncio.create_task(
            bot.send_message(
                LOG_CHAT_ID,
                "#started_streaming\n"
                f"• Title: {song_info.get('title','Unknown')}\n"
                f"• Duration: {song_info.get('duration','Unknown')}\n"
                f"• Requested by: {song_info.get('requester','Unknown')}\n"
                f"• Mode: local"
            )
        )
    except Exception as e:
        print(f"Error during fallback local playback in chat {chat_id}: {e}")
        await bot.send_message(
            chat_id,
            f"❌ Failed to play “{song_info.get('title','Unknown')}” locally: {e}"
        )
        if chat_id in chat_containers and chat_containers[chat_id]:
            chat_containers[chat_id].pop(0)

@bot.on_callback_query()
async def callback_query_handler(client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    user_id = callback_query.from_user.id
    data = callback_query.data
    user = callback_query.from_user
    if not await deterministic_privilege_validator(callback_query):
        await callback_query.answer("❌ You need to be an admin to use this button.", show_alert=True)
        return
    if data == "pause":
        try:
            await call_py.pause(chat_id)
            await callback_query.answer("⏸ Playback paused.")
            await client.send_message(chat_id, f"⏸ Playback paused by {user.first_name}.")
        except Exception as e:
            await callback_query.answer("❌ Error pausing playback.", show_alert=True)
    elif data == "resume":
        try:
            await call_py.resume(chat_id)
            await callback_query.answer("▶️ Playback resumed.")
            await client.send_message(chat_id, f"▶️ Playback resumed by {user.first_name}.")
        except Exception as e:
            await callback_query.answer("❌ Error resuming playback.", show_alert=True)
    elif data == "skip":
        if chat_id not in chat_containers or not chat_containers[chat_id]:
            await callback_query.answer("❌ No songs in the queue to skip.", show_alert=True)
            return
        skipped_song = chat_containers[chat_id].pop(0)
        try:
            if skipped_song.get('file_path'):
                os.remove(skipped_song['file_path'])
        except Exception:
            pass
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        await callback_query.answer("⏩ Skipped! Playing next song...")
        await client.send_message(chat_id, f"⏩ {user.first_name} skipped **{skipped_song['title']}**.")
        if chat_id in chat_containers and chat_containers[chat_id]:
            next_song_info = chat_containers[chat_id][0]
            try:
                dummy_msg = await bot.send_message(chat_id, f"🎧 Preparing next song: **{next_song_info['title']}** ...")
                await fallback_local_playback(chat_id, dummy_msg, next_song_info)
            except Exception as e:
                print(f"Error starting next local playback: {e}")
                await bot.send_message(chat_id, f"❌ Failed to start next song: {e}")
        else:
            await safe_leave_call(chat_id)
            await client.send_message(chat_id, "❌ No more songs in the queue.")
    elif data == "clear":
        await clean_chat_resources(chat_id)
        await safe_leave_call(chat_id)
        await callback_query.answer("🗑️ Cleared the queue.")
        await callback_query.message.edit("🗑️ Cleared the queue.")
    elif data == "stop":
        await clean_chat_resources(chat_id)
        await safe_leave_call(chat_id)
        await callback_query.answer("🛑 Playback stopped and queue cleared.")
        await client.send_message(chat_id, f"🛑 Playback stopped and queue cleared by {user.first_name}.")

async def get_recommended_songs(query: str):
    try:
        url = f"https://teenage-liz-frozzennbotss-61567ab4.koyeb.app/search?title={query}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("playlist", [])
    except Exception as e:
        print(f"Failed to fetch recommendations: {e}")
        return []

@call_py.on_update(fl.stream_end())
async def stream_end_handler(_: PyTgCalls, update: StreamEnded):
    chat_id = update.chat_id
    if chat_id in chat_containers and chat_containers[chat_id]:
        skipped_song = chat_containers[chat_id].pop(0)
        try:
            if skipped_song.get('file_path'):
                os.remove(skipped_song['file_path'])
        except Exception as e:
            print(f"Error deleting file: {e}")
        if chat_id in chat_containers and chat_containers[chat_id]:
            next_song_info = chat_containers[chat_id][0]
            try:
                dummy_msg = await bot.send_message(chat_id, f"🎧 Preparing next song: **{next_song_info['title']}** ...")
                await fallback_local_playback(chat_id, dummy_msg, next_song_info)
            except Exception as e:
                print(f"Error starting next local playback: {e}")
                await bot.send_message(chat_id, f"❌ Failed to start next song: {e}")
        else:
            await safe_leave_call(chat_id)
            await bot.send_message(chat_id, "❌ No more songs in the queue.")
            await bot.send_message(chat_id, "Queue is empty. Here are some recommendations:")
            recommendations = await get_recommended_songs(skipped_song['title'])
            if recommendations:
                recommend_text = "\n".join([f"**»** `{rec['title']}`" for rec in recommendations[:5]])
                buttons = [
                    [InlineKeyboardButton(f"{i+1}", callback_data=f"recplay_{rec['link']}_{rec['title']}_{rec['duration']}_{rec['thumbnail']}") for i, rec in enumerate(recommendations[:5])]
                ]
                await bot.send_message(chat_id, f"**Top 5 Recommendations:**\n{recommend_text}", reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_message(filters.group & filters.command(["stop", "end"]))
async def stop_handler(client, message: Message):
    chat_id = message.chat.id
    if not await deterministic_privilege_validator(message):
        await message.reply("❌ You need to be an admin to use this command.")
        return
    await clean_chat_resources(chat_id)
    await safe_leave_call(chat_id)
    await message.reply("⏹ Stopped the music and cleared the queue.")

@bot.on_message(filters.group & filters.command("pause"))
async def pause_handler(client, message: Message):
    chat_id = message.chat.id
    if not await deterministic_privilege_validator(message):
        await message.reply("❌ You need to be an admin to use this command.")
        return
    try:
        await call_py.pause(chat_id)
        await message.reply("⏸ Paused the stream.")
    except Exception as e:
        await message.reply(f"❌ Failed to pause the stream.\nError: {str(e)}")

@bot.on_message(filters.group & filters.command("resume"))
async def resume_handler(client, message: Message):
    chat_id = message.chat.id
    if not await deterministic_privilege_validator(message):
        await message.reply("❌ You need to be an admin to use this command.")
        return
    try:
        await call_py.resume(chat_id)
        await message.reply("▶️ Resumed the stream.")
    except Exception as e:
        await message.reply(f"❌ Failed to resume the stream.\nError: {str(e)}")

@bot.on_message(filters.group & filters.command("skip"))
async def skip_handler(client, message: Message):
    chat_id = message.chat.id
    if not await deterministic_privilege_validator(message):
        await message.reply("❌ You need to be an admin to use this command.")
        return
    status_message = await message.reply("⏩ Skipping the current song...")
    if chat_id not in chat_containers or not chat_containers[chat_id]:
        await status_message.edit("❌ No songs in the queue to skip.")
        return
    skipped_song = chat_containers[chat_id].pop(0)
    try:
        if skipped_song.get('file_path'):
            os.remove(skipped_song['file_path'])
    except Exception as e:
        print(f"Error deleting file: {e}")
    try:
        await call_py.leave_call(chat_id)
    except Exception:
        pass
    if not chat_containers.get(chat_id):
        await status_message.edit(f"⏩ Skipped **{skipped_song['title']}**.\n\n😔 No more songs in the queue.")
        return
    next_song_info = chat_containers[chat_id][0]
    await status_message.edit(f"⏩ Skipped **{skipped_song['title']}**.\n\n💕 Playing the next song...")
    try:
        dummy_msg = await client.send_message(chat_id, f"🎧 Preparing next song: **{next_song_info['title']}** ...")
        await fallback_local_playback(chat_id, dummy_msg, next_song_info)
    except Exception as e:
        print(f"Error starting next local playback: {e}")
        await client.send_message(chat_id, f"❌ Failed to start next song: {e}")

@bot.on_message(filters.command("reboot"))
async def reboot_handler(_, message: Message):
    chat_id = message.chat.id
    await clean_chat_resources(chat_id)
    await safe_leave_call(chat_id)
    await message.reply("♻️ Rebooted for this chat. All data for this chat has been cleared.")

@bot.on_message(filters.command("ping"))
async def ping_handler(_, message: Message):
    try:
        current_time = time.time()
        uptime_seconds = int(current_time - bot_start_time)
        uptime_str = str(timedelta(seconds=uptime_seconds))
        cpu_usage = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        ram_usage = f"{memory.used // (1024 ** 2)}MB / {memory.total // (1024 ** 2)}MB ({memory.percent}%)"
        disk = psutil.disk_usage('/')
        disk_usage = f"{disk.used // (1024 ** 3)}GB / {disk.total // (1024 ** 3)}GB ({disk.percent}%)"
        response = (
            f"🏓 **Pong!**\n\n"
            f"**Local Server Stats:**\n"
            f"• **Uptime:** `{uptime_str}`\n"
            f"• **CPU Usage:** `{cpu_usage}%`\n"
            f"• **RAM Usage:** `{ram_usage}`\n"
            f"• **Disk Usage:** `{disk_usage}`"
        )
        await message.reply(response)
    except Exception as e:
        await message.reply(f"❌ Failed to execute the command.\nError: {str(e)}\n\nSupport: @bestshayri_raj")

@bot.on_message(filters.group & filters.command("clear"))
async def clear_handler(_, message: Message):
    chat_id = message.chat.id
    if not await deterministic_privilege_validator(message):
        await message.reply("❌ You need to be an admin to use this command.")
        return
    if chat_id in chat_containers:
        for song in chat_containers[chat_id]:
            try:
                if song.get('file_path'):
                    os.remove(song['file_path'])
            except Exception as e:
                print(f"Error deleting file: {e}")
        chat_containers.pop(chat_id)
        await message.reply("🗑️ Cleared the queue.")
    else:
        await message.reply("❌ No songs in the queue to clear.")

@bot.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_handler(_, message: Message):
    if not message.reply_to_message:
        await message.reply("❌ Please reply to the message you want to broadcast.")
        return
    broadcast_message = message.reply_to_message
    all_chats = list(broadcast_collection.find({}))
    success = 0
    failed = 0
    for chat in all_chats:
        try:
            target_chat_id = int(chat.get("chat_id"))
            await bot.forward_messages(
                chat_id=target_chat_id,
                from_chat_id=broadcast_message.chat.id,
                message_ids=broadcast_message.id
            )
            success += 1
        except Exception as e:
            print(f"Failed to broadcast to {target_chat_id}: {e}")
            failed += 1
        await asyncio.sleep(1)
    await message.reply(f"Broadcast complete!\n✅ Success: {success}\n❌ Failed: {failed}")

def save_state_to_db():
    data = {
        "chat_containers": { str(cid): queue for cid, queue in chat_containers.items() }
    }
    state_backup.replace_one(
        {"_id": "singleton"},
        {"_id": "singleton", "state": data},
        upsert=True
    )
    chat_containers.clear()

def load_state_from_db():
    doc = state_backup.find_one_and_delete({"_id": "singleton"})
    if not doc or "state" not in doc:
        return
    data = doc["state"]
    for cid_str, queue in data.get("chat_containers", {}).items():
        try:
            chat_containers[int(cid_str)] = queue
        except ValueError:
            continue

class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is running!")
        elif self.path == "/status":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot status: Running")
        elif self.path == "/restart":
            save_state_to_db()
            os.execl(sys.executable, sys.executable, *sys.argv)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/webhook":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                update = json.loads(body)
                asyncio.run_coroutine_threadsafe(
                    bot._process_update(update), asyncio.get_event_loop()
                )
            except Exception as e:
                print("Error processing update:", e)
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("", port), WebhookHandler)
    print(f"HTTP server running on port {port}")
    server.serve_forever()

threading.Thread(target=run_http_server, daemon=True).start()

logger = logging.getLogger(__name__)

frozen_check_ok = asyncio.Event()

async def frozen_check_handler(_, message: Message):
    if message.from_user.username == ASSISTANT_USERNAME:
        frozen_check_ok.set()
        print("Received frozen check acknowledgment.")

async def frozen_check_loop(bot_username: str):
    while True:
        try:
            frozen_check_ok.clear()
            await assistant.send_message(bot_username, "/frozen_check")
            print(f"Sent /frozen_check to @{bot_username}")
            try:
                await asyncio.wait_for(frozen_check_ok.wait(), timeout=30)
            except asyncio.TimeoutError:
                print("No frozen check reply within 30 seconds. Restarting bot.")
                await restart_bot()
        except Exception as e:
            logger.error(f"Error in frozen_check_loop: {e}")
        await asyncio.sleep(60)

async def restart_bot():
    port = int(os.environ.get("PORT", 8080))
    url = f"http://localhost:{port}/restart"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    logger.info("Local restart endpoint triggered successfully.")
                else:
                    logger.error(f"Local restart endpoint failed: {resp.status}")
    except Exception as e:
        logger.error(f"Error calling local restart endpoint: {e}")

@bot.on_callback_query(filters.regex("^recplay_"))
async def recommend_play_handler(_, callback_query: CallbackQuery):
    try:
        data_parts = callback_query.data.split('_')
        link = data_parts[1]
        title = data_parts[2]
        duration = data_parts[3]
        thumb = data_parts[4]
        song_info = {
            "url": link,
            "title": title,
            "duration": iso8601_to_human_readable(duration),
            "duration_seconds": isodate.parse_duration(duration).total_seconds(),
            "requester": callback_query.from_user.first_name,
            "thumbnail": thumb,
            'is_local': False
        }
        chat_id = callback_query.message.chat.id
        chat_containers.setdefault(chat_id, []).append(song_info)
        await callback_query.message.edit_text(f"✨ Added `{song_info['title']}` to the queue.")
        if len(chat_containers[chat_id]) == 1:
            dummy_msg = await bot.send_message(chat_id, f"🎧 Preparing recommended song: **{song_info['title']}** ...")
            await fallback_local_playback(chat_id, dummy_msg, song_info)
    except Exception as e:
        await callback_query.answer(f"❌ Error adding song: {e}", show_alert=True)

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Loading persisted state from MongoDB...")
    load_state_from_db()
    logger.info("State loaded successfully.")
    logger.info("→ Starting PyTgCalls client...")
    call_py.start()
    logger.info("PyTgCalls client started.")
    logger.info("→ Starting Telegram bot client (bot.start)...")
    try:
        bot.start()
    except Exception as e:
        logger.error(f"❌ Failed to start Pyrogram client: {e}")
        sys.exit(1)
    me = bot.get_me()
    BOT_NAME = me.first_name or "Frozen Music"
    BOT_USERNAME = me.username or os.getenv("BOT_USERNAME", "vcmusiclubot")
    BOT_LINK = f"https://t.me/{BOT_USERNAME}"
    logger.info(f"✅ Bot Name: {BOT_NAME!r}")
    logger.info(f"✅ Bot Username: {BOT_USERNAME}")
    logger.info(f"✅ Bot Link: {BOT_LINK}")
    asyncio.get_event_loop().create_task(frozen_check_loop(BOT_USERNAME))
    if not assistant.is_connected:
        logger.info("Assistant not connected; starting assistant client...")
        assistant.run()
        logger.info("Assistant client connected.")
    try:
        assistant_user = assistant.get_me()
        ASSISTANT_USERNAME = assistant_user.username
        ASSISTANT_CHAT_ID = assistant_user.id
        logger.info(f"✨ Assistant Username: {ASSISTANT_USERNAME}")
        logger.info(f"💕 Assistant Chat ID: {ASSISTANT_CHAT_ID}")
        asyncio.get_event_loop().run_until_complete(precheck_channels(assistant))
        logger.info("✅ Assistant precheck completed.")
    except Exception as e:
        logger.error(f"❌ Failed to fetch assistant info: {e}")
    logger.info("→ Entering idle() (long-polling)")
    idle()
    bot.stop()
    logger.info("Bot stopped.")
    logger.info("✅ All services are up and running. Bot started successfully.")

async def is_admin_or_owner(client: Client, chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status in [ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR]:
            return True
        return False
    except Exception as e:
        print(f"Error checking admin status: {e}")
        return False

# --- Core Bot Functions (from previous code) ---
async def process_pending_command(chat_id, delay):
    await asyncio.sleep(delay)
    if chat_id in chat_pending_commands:
        message, cooldown_reply = chat_pending_commands.pop(chat_id)
        if cooldown_reply:
            try:
                await cooldown_reply.delete()
            except RPCError:
                pass
        await play_handler(bot, message)

async def clean_chat_resources(chat_id: int):
    if chat_id in playback_tasks:
        try:
            playback_tasks[chat_id].cancel()
        except Exception:
            pass
        del playback_tasks[chat_id]
    if chat_id in chat_containers:
        for song in chat_containers[chat_id]:
            try:
                if song.get('file_path'):
                    os.remove(song['file_path'])
            except Exception as e:
                print(f"Error deleting file for chat {chat_id}: {e}")
        del chat_containers[chat_id]
    chat_last_command.pop(chat_id, None)
    chat_pending_commands.pop(chat_id, None)
    playback_mode.pop(chat_id, None)

async def safe_leave_call(chat_id: int):
    try:
        await call_py.leave_call(chat_id)
    except Exception as e:
        print(f"Error leaving the voice chat: {e}")

def iso8601_to_human_readable(iso_duration):
    try:
        duration = isodate.parse_duration(iso_duration)
        total_seconds = int(duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}:{minutes:02}:{seconds:02}"
        return f"{minutes}:{seconds:02}"
    except Exception as e:
        return "Unknown duration"

async def fetch_youtube_link(query):
    try:
        url = f"https://teenage-liz-frozzennbotss-61567ab4.koyeb.app/search?title={query}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if "playlist" in data:
                        return data
                    return (
                        data.get("link"),
                        data.get("title"),
                        data.get("duration"),
                        data.get("thumbnail")
                    )
                else:
                    raise Exception(f"API returned status code {response.status}")
    except Exception as e:
        raise Exception(f"Failed to fetch YouTube link: {str(e)}")

async def fetch_youtube_link_backup(query):
    if not BACKUP_SEARCH_API_URL:
        raise Exception("Backup Search API URL not configured")
    backup_url = (
        f"{BACKUP_SEARCH_API_URL.rstrip('/')}"
        f"/search?title={urllib.parse.quote(query)}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(backup_url, timeout=30) as resp:
                if resp.status != 200:
                    raise Exception(f"Backup API returned status {resp.status}")
                data = await resp.json()
                if "playlist" in data:
                    return data
                return (
                    data.get("link"),
                    data.get("title"),
                    data.get("duration"),
                    data.get("thumbnail")
                )
    except Exception as e:
        raise Exception(f"Backup Search API error: {e}")

BOT_NAME = os.environ.get("BOT_NAME", "Frozen Music")
BOT_LINK = os.environ.get("BOT_LINK", "https://t.me/vcmusiclubot")

async def is_assistant_in_chat(chat_id):
    try:
        member = await assistant.get_chat_member(chat_id, ASSISTANT_USERNAME)
        return member.status is not None
    except Exception as e:
        error_message = str(e)
        if "USER_BANNED" in error_message or "Banned" in error_message:
            return "banned"
        elif "USER_NOT_PARTICIPANT" in error_message or "Chat not found" in error_message:
            return False
        print(f"Error checking assistant in chat: {e}")
        return False

def to_bold_unicode(text: str) -> str:
    bold_text = ""
    for char in text:
        if 'A' <= char <= 'Z':
            bold_text += chr(ord('𝗔') + (ord(char) - ord('A')))
        elif 'a' <= char <= 'z':
            bold_text += chr(ord('𝗮') + (ord(char) - ord('a')))
        else:
            bold_text += char
    return bold_text

@bot.on_message(filters.new_chat_members)
async def welcome_handler(_, message: Message):
    for member in message.new_chat_members:
        if member.is_bot:
            continue

        chat_id = message.chat.id
        chat_name = message.chat.title
        user_name = member.first_name

        welcome_settings = welcome_db.find_one({"chat_id": chat_id})
        custom_message = welcome_settings.get("message") if welcome_settings else None
        custom_sticker = welcome_settings.get("sticker_id") if welcome_settings else None

        if custom_message:
            welcome_text = custom_message.format(name=user_name, group_name=chat_name)
        else:
            welcome_text = (
                f"🎉 Welcome, **{user_name}** to **{chat_name}**!\n"
                f"We hope you enjoy your time here. Feel free to use my music commands like `/play`."
            )

        try:
            sticker_id = custom_sticker or WELCOME_STICKER_ID
            await bot.send_sticker(chat_id, sticker=sticker_id)
        except RPCError:
            print("Failed to send welcome sticker, falling back to text.")
            
        await bot.send_message(chat_id, welcome_text, parse_mode=ParseMode.MARKDOWN)

@bot.on_message(filters.command("start"))
async def start_handler(_, message: Message):
    user_id = message.from_user.id
    raw_name = message.from_user.first_name or ""
    styled_name = to_bold_unicode(raw_name)
    user_link = f"[{styled_name}](tg://user?id={user_id})"

    add_me_text = to_bold_unicode("Add Me")
    updates_text = to_bold_unicode("Updates")
    support_text = to_bold_unicode("Support")
    help_text = to_bold_unicode("Help")

    caption = (
        f"👋 нєу {user_link} 💠, 🥀\n\n"
        f"🎶 𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 『 {BOT_NAME.upper()} 』 🎵\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🚀 𝐓𝐨𝐩-𝐍𝐨𝐭𝐜𝐡 24×7 𝐔𝐩𝐭𝐢𝐦𝐞 ⚡\n"
        "🔊 𝐂𝐫𝐲𝐬𝐭𝐚𝐥-𝐂𝐥𝐞𝐚𝐫 𝐀𝐮𝐝𝐢𝐨 🎼\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🎧 𝐒𝐮𝐩𝐩𝐨𝐫𝐭𝐞𝐝 𝐏𝐥𝐚𝐭𝐟𝐨𝐫𝐦𝐬:\n"
        "   • YouTube 🎥\n"
        "   • Spotify 🎵\n"
        "   • Resso 🎶\n"
        "   • Apple Music 🍎\n"
        "   • SoundCloud ☁️\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "✨ 𝐒𝐦𝐚𝐫𝐭 𝐀𝐮𝐭𝐨-𝐒𝐮𝐠𝐠𝐞𝐬𝐭𝐢𝐨𝐧𝐬 𝙬𝙝𝙚𝙣 𝐪𝐮𝐞𝐮𝐞 𝐞𝐧𝐝𝐬 🔄\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🛠️ 𝐀𝐝𝐦𝐢𝐧 𝐂𝐨𝐦𝐦𝐚𝐧𝐝𝐬:\n"
        "   ▸ Pause ⏸️ | Resume ▶️ | Skip ⏭️\n"
        "   ▸ Stop ⏹️ | Mute 🔇 | Unmute 🔊\n"
        "   ▸ Tmute ⏲️ | Kick 👢 | Ban 🚫 | Unban ✅\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "❤️ 𝐂𝐨𝐮𝐩𝐥𝐞 𝐒𝐮𝐠𝐠𝐞𝐬𝐭𝐢𝐨𝐧 💕\n"
        "   (ʀᴀɴᴅᴏᴍ ᴘᴀɪʀ ɪɴ ɢʀᴏᴜᴘ)\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"๏ ᴄʟɪᴄᴋ {help_text} ʙᴇʟᴏᴡ ғᴏʀ ғᴜʟʟ ᴄᴏᴍᴍᴀɴᴅ ʟɪsᴛ 📜"
    )

    buttons = [
        [
            InlineKeyboardButton(f"➕ {add_me_text}", url=f"{BOT_LINK}?startgroup=true"),
            InlineKeyboardButton(f"📢 {updates_text}", url="https://t.me/bestshayri_raj")
        ],
        [
            InlineKeyboardButton(f"💬 {support_text}", url="https://t.me/+34oz1KeknQtlYTdl"),
            InlineKeyboardButton(f"❓ {help_text}", callback_data="show_help")
        ],
        [
            InlineKeyboardButton("👑 Owner", url="https://t.me/teamrajweb"),
            InlineKeyboardButton("⚡ Earning Zone", url="https://t.me/RAJCOMMITMENTBOT")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)

    await message.reply_animation(
        animation="https://filehosting.kustbotsweb.workers.dev/tfq.mp4",
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )

    if message.chat.type == ChatType.PRIVATE:
        if not broadcast_collection.find_one({"chat_id": user_id}):
            broadcast_collection.insert_one({"chat_id": user_id, "type": "private"})
    elif message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if not broadcast_collection.find_one({"chat_id": message.chat.id}):
            broadcast_collection.insert_one({"chat_id": message.chat.id, "type": "group"})

@bot.on_callback_query(filters.regex("^go_back$"))
async def go_back_callback(_, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    raw_name = callback_query.from_user.first_name or ""
    styled_name = to_bold_unicode(raw_name)
    user_link = f"[{styled_name}](tg://user?id={user_id})"
    add_me_text = to_bold_unicode("Add Me")
    updates_text = to_bold_unicode("Updates")
    support_text = to_bold_unicode("Support")
    help_text = to_bold_unicode("Help")
    caption = (
        f"👋 нєу {user_link} 💠, 🥀\n\n"
        f"🎶 𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 『 {BOT_NAME.upper()} 』 🎵\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🚀 𝐓𝐨𝐩-𝐍𝐨𝐭𝐜𝐡 24×7 𝐔p𝐭𝐢𝐦𝐞 ⚡\n"
        "🔊 𝐂𝐫𝐲𝐬𝐭𝐚𝐥-𝐂𝐥𝐞𝐚𝐫 𝐀𝐮𝐝𝐢𝐨 🎼\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🎧 𝐒𝐮𝐩𝐩𝐨𝐫𝐭𝐞𝐝 𝐏𝐥𝐚𝐭𝐟𝐨𝐫𝐦𝐬:\n"
        "   • YouTube 🎥\n"
        "   • Spotify 🎵\n"
        "   • Resso 🎶\n"
        "   • Apple Music 🍎\n"
        "   • SoundCloud ☁️\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "✨ 𝐒𝐦𝐚𝐫𝐭 𝐀𝐮𝐭𝐨-𝐒𝐮𝐠𝐠𝐞𝐬𝐭𝐢𝐨𝐧𝐬 𝙬𝙝𝙚𝙣 𝐪𝐮𝐞𝐮𝐞 𝐞𝐧𝐝𝐬 🔄\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🛠️ 𝐀𝐝𝐦𝐢𝐧 𝐂𝐨𝐦𝐦𝐚𝐧𝐝𝐬:\n"
        "   ▸ Pause ⏸️ | Resume ▶️ | Skip ⏭️\n"
        "   ▸ Stop ⏹️ | Mute 🔇 | Unmute 🔊\n"
        "   ▸ Tmute ⏲️ | Kick 👢 | Ban 🚫 | Unban ✅\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "❤️ 𝐂𝐨𝐮𝐩𝐥𝐞 𝐒𝐮𝐠𝐠𝐞𝐬𝐭𝐢𝐨𝐧 💕\n"
        "   (ʀᴀɴᴅᴏᴍ ᴘᴀɪʀ ɪɴ ɢʀᴏᴜᴘ)\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"๏ ᴄʟɪᴄᴋ {help_text} ʙᴇʟᴏᴡ ғᴏʀ ғᴜʟʟ ᴄᴏᴍᴍᴀɴᴅ ʟɪsᴛ 📜"
    )
    buttons = [
        [
            InlineKeyboardButton(f"➕ {add_me_text}", url=f"{BOT_LINK}?startgroup=true"),
            InlineKeyboardButton(f"📢 {updates_text}", url="https://t.me/bestshayri_raj")
        ],
        [
            InlineKeyboardButton(f"💬 {support_text}", url="https://t.me/+34oz1KeknQtlYTdl"),
            InlineKeyboardButton(f"❓ {help_text}", callback_data="show_help")
        ],
        [
            InlineKeyboardButton("👑 Owner", url="https://t.me/teamrajweb"),
            InlineKeyboardButton("⚡ Earning Zone", url="https://t.me/RAJCOMMITMENTBOT")
        ]
    ]
    await callback_query.message.edit_caption(
        caption=caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@bot.on_callback_query(filters.regex("^show_help$"))
async def show_help_callback(_, callback_query: CallbackQuery):
    help_text = ">📜 *Choose a category to explore commands:*"
    buttons = [
        [
            InlineKeyboardButton("🎵 Music Controls", callback_data="help_music"),
            InlineKeyboardButton("🛡️ Admin Tools", callback_data="help_admin")
        ],
        [
            InlineKeyboardButton("❤️ Couple Suggestion", callback_data="help_couple"),
            InlineKeyboardButton("🔍 Utility", callback_data="help_util")
        ],
        [
            InlineKeyboardButton("🏠 Home", callback_data="go_back")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

@bot.on_callback_query(filters.regex("^help_music$"))
async def help_music_callback(_, callback_query: CallbackQuery):
    text = (
        ">🎵 *Music & Playback Commands*\n\n"
        ">➜ `/play <song name or URL>`\n"
        "   • Play a song (YouTube/Spotify/Resso/Apple Music/SoundCloud).\n"
        "   • If replied to an audio/video, plays it directly.\n\n"
        ">➜ `/queue`\n"
        "   • View the current queue and jump to a song.\n\n"
        ">➜ `/saveplaylist <name>`\n"
        "   • Save the current queue to your personal playlist.\n\n"
        ">➜ `/loadplaylist <name>`\n"
        "   • Load and play a saved playlist.\n\n"
        ">➜ `/myplaylists`\n"
        "   • View your saved playlists.\n\n"
        ">➜ `/skip`\n"
        "   • Skip the currently playing song. (Admins only)\n\n"
        ">➜ `/pause`\n"
        "   • Pause the current stream. (Admins only)\n\n"
        ">➜ `/resume`\n"
        "   • Resume a paused stream. (Admins only)\n\n"
        ">➜ `/stop` or `/end`\n"
        "   • Stop playback and clear the queue. (Admins only)"
    )
    buttons = [[InlineKeyboardButton("🔙 Back", callback_data="show_help")]]
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_callback_query(filters.regex("^help_admin$"))
async def help_admin_callback(_, callback_query: CallbackQuery):
    text = (
        "🛡️ *Admin & Moderation Commands*\n\n"
        ">➜ `/mute`\n"
        "   • Mute a user indefinitely.\n\n"
        ">➜ `/unmute`\n"
        "   • Unmute a previously muted user.\n\n"
        ">➜ `/tmute <time>`\n"
        "   • Temporarily mute for a set duration. (e.g. `10m`, `2h`).\n\n"
        ">➜ `/kick`\n"
        "   • Kick (ban + unban) a user immediately.\n\n"
        ">➜ `/ban`\n"
        "   • Ban a user.\n\n"
        ">➜ `/unban`\n"
        "   • Unban a previously banned user.\n\n"
        ">➜ `/welcomeset <text>`\n"
        "   • Set a custom welcome message for new users. Use `{name}` and `{group_name}`.\n\n"
        ">➜ `/welcomesticker`\n"
        "   • Reply to a sticker to set it as the welcome sticker."
    )
    buttons = [[InlineKeyboardButton("🔙 Back", callback_data="show_help")]]
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


@bot.on_callback_query(filters.regex("^help_couple$"))
async def help_couple_callback(_, callback_query: CallbackQuery):
    text = (
        "❤️ *Couple Suggestion Command*\n\n"
        ">➜ `/couple`\n"
        "   • Picks two random non-bot members and posts a “couple” image with their names.\n"
        "   • Caches daily so the same pair appears until midnight UTC.\n"
        "   • Uses per-group member cache for speed."
    )
    buttons = [[InlineKeyboardButton("🔙 Back", callback_data="show_help")]]
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


@bot.on_callback_query(filters.regex("^help_util$"))
async def help_util_callback(_, callback_query: CallbackQuery):
    text = (
        "🔍 *Utility & Extra Commands*\n\n"
        ">➜ `/ping`\n"
        "   • Check bot’s response time and uptime.\n\n"
        ">➜ `/clear`\n"
        "   • Clear the entire queue. (Admins only)\n\n"
        ">➜ Auto-Suggestions:\n"
        "   • When the queue ends, the bot automatically suggests new songs via inline buttons.\n\n"
        ">➜ *Audio Quality & Limits*\n"
        "   • Streams up to 2 hours 10 minutes, but auto-fallback for longer. (See `MAX_DURATION_SECONDS`)\n"
    )
    buttons = [[InlineKeyboardButton("🔙 Back", callback_data="show_help")]]
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_message(filters.group & filters.command("mute"))
async def mute_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to mute them.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.restrict_chat_member(
            message.chat.id,
            target_user_id,
            ChatPermissions()
        )
        await message.reply(f"🔇 **{target_user_name}** has been muted.")
    except Exception as e:
        await message.reply(f"❌ Failed to mute user: {e}")

@bot.on_message(filters.group & filters.command("tmute"))
async def tmute_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to mute them.")

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        return await message.reply("❌ Usage: `/tmute <time>`. Example: `/tmute 10m`.")

    time_str = parts[1]
    
    # Simple time parsing (e.g., 10m, 2h, 1d)
    match = re.match(r"(\d+)([mhd])", time_str)
    if not match:
        return await message.reply("❌ Invalid time format. Use `m` for minutes, `h` for hours, `d` for days.")
        
    value, unit = int(match.group(1)), match.group(2)
    if unit == 'm':
        mute_until = datetime.now() + timedelta(minutes=value)
    elif unit == 'h':
        mute_until = datetime.now() + timedelta(hours=value)
    elif unit == 'd':
        mute_until = datetime.now() + timedelta(days=value)
    else:
        return await message.reply("❌ Invalid time unit. Use `m`, `h`, or `d`.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.restrict_chat_member(
            message.chat.id,
            target_user_id,
            ChatPermissions(can_send_messages=False),
            until_date=mute_until
        )
        await message.reply(f"🔇 **{target_user_name}** has been muted for {value}{unit}.")
    except Exception as e:
        await message.reply(f"❌ Failed to mute user: {e}")


@bot.on_message(filters.group & filters.command("unmute"))
async def unmute_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to unmute them.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.restrict_chat_member(
            message.chat.id,
            target_user_id,
            ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        )
        await message.reply(f"🔊 **{target_user_name}** has been unmuted.")
    except Exception as e:
        await message.reply(f"❌ Failed to unmute user: {e}")

@bot.on_message(filters.group & filters.command("ban"))
async def ban_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to ban them.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.ban_chat_member(message.chat.id, target_user_id)
        await message.reply(f"🚫 **{target_user_name}** has been banned.")
    except Exception as e:
        await message.reply(f"❌ Failed to ban user: {e}")

@bot.on_message(filters.group & filters.command("unban"))
async def unban_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to unban them.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.unban_chat_member(message.chat.id, target_user_id)
        await message.reply(f"✅ **{target_user_name}** has been unbanned.")
    except Exception as e:
        await message.reply(f"❌ Failed to unban user: {e}")

@bot.on_message(filters.group & filters.command("kick"))
async def kick_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message:
        return await message.reply("❌ Reply to a user's message to kick them.")

    target_user_id = message.reply_to_message.from_user.id
    target_user_name = message.reply_to_message.from_user.first_name

    try:
        await bot.ban_chat_member(message.chat.id, target_user_id)
        await bot.unban_chat_member(message.chat.id, target_user_id)
        await message.reply(f"👢 **{target_user_name}** has been kicked from the group.")
    except Exception as e:
        await message.reply(f"❌ Failed to kick user: {e}")

@bot.on_message(filters.group & filters.command("welcomeset"))
async def welcomeset_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return await message.reply("❌ Please provide a custom welcome message.\nExample: `/welcomeset Welcome {name} to {group_name}!`")

    custom_message = text[1].strip()
    chat_id = message.chat.id

    welcome_db.update_one(
        {"chat_id": chat_id},
        {"$set": {"message": custom_message}},
        upsert=True
    )
    await message.reply("✅ Welcome message has been updated!")

@bot.on_message(filters.group & filters.command("welcomesticker"))
async def welcomesticker_handler(_, message: Message):
    if not await is_admin_or_owner(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ You must be an admin to use this command.")

    if not message.reply_to_message or not message.reply_to_message.sticker:
        return await message.reply("❌ Reply to a sticker to set it as the welcome sticker.")

    sticker_id = message.reply_to_message.sticker.file_id
    chat_id = message.chat.id

    welcome_db.update_one(
        {"chat_id": chat_id},
        {"$set": {"sticker_id": sticker_id}},
        upsert=True
    )
    await message.reply("✅ Welcome sticker has been updated!")


@bot.on_message(filters.group & filters.regex(r'^/play(?:@\w+)?(?:\s+(?P<query>.+))?$'))
async def play_handler(_, message: Message):
    # (Existing play_handler code)
    chat_id = message.chat.id

    if message.reply_to_message and (message.reply_to_message.audio or message.reply_to_message.video):
        processing_message = await message.reply("❄️")
        orig = message.reply_to_message
        media = orig.video or orig.audio
        if getattr(media, 'file_size', 0) > 100 * 1024 * 1024:
            await processing_message.edit("❌ Audio file too large. Maximum allowed size is 100MB.")
            return
        await processing_message.edit("⏳ Please wait, downloading audio…")
        try:
            file_path = await bot.download_media(media)
        except Exception as e:
            await processing_message.edit(f"❌ Failed to download media: {e}")
            return
        thumb_path = None
        try:
            thumbs = media.thumbs
            if thumbs:
                thumb_path = await bot.download_media(thumbs[0])
        except Exception:
            pass
        duration = media.duration or 0
        title = getattr(media, 'file_name', 'Untitled')
        song_info = {
            'url': file_path,
            'title': title,
            'duration': format_time(duration),
            'duration_seconds': duration,
            'requester': message.from_user.first_name,
            'thumbnail': thumb_path,
            'is_local': True
        }
        chat_containers.setdefault(chat_id, []).append(song_info)
        if len(chat_containers[chat_id]) == 1:
            await fallback_local_playback(chat_id, processing_message, song_info)
        else:
            await processing_message.edit(f"✨ Added `{song_info['title']}` to the queue at position {len(chat_containers[chat_id])}")
        return

    match = message.matches[0]
    query = (match.group('query') or "").strip()
    try:
        await message.delete()
    except RPCError:
        pass
    now_ts = time.time()
    if chat_id in chat_last_command and (now_ts - chat_last_command[chat_id]) < COOLDOWN:
        remaining = int(COOLDOWN - (now_ts - chat_last_command[chat_id]))
        if chat_id in chat_pending_commands:
            await bot.send_message(chat_id, f"⏳ A command is already queued for this chat. Please wait {remaining}s.")
        else:
            cooldown_reply = await bot.send_message(chat_id, f"⏳ On cooldown. Processing in {remaining}s.")
            chat_pending_commands[chat_id] = (message, cooldown_reply)
            asyncio.create_task(process_pending_command(chat_id, remaining))
        return
    chat_last_command[chat_id] = now_ts
    if not query:
        await bot.send_message(
            chat_id,
            "❌ You did not specify a song.\n\n"
            "Correct usage: /play <song name>\nExample: /play shape of you"
        )
        return
    await process_play_command(message, query)


async def process_play_command(message: Message, query: str):
    chat_id = message.chat.id
    processing_message = await message.reply("❄️")
    status = await is_assistant_in_chat(chat_id)
    if status == "banned":
        await processing_message.edit("❌ Assistant is banned from this chat.")
        return
    if status is False:
        invite_link = await bot.export_chat_invite_link(chat_id)
        if not invite_link:
            await processing_message.edit("❌ Could not obtain an invite link to add the assistant.")
            return
        try:
            await assistant.join_chat(invite_link)
            await processing_message.edit("✅ Assistant has joined the chat.")
        except Exception as e:
            await processing_message.edit(f"❌ Failed to invite assistant: {e}")
            return
    if "youtu.be" in query:
        m = re.search(r"youtu\.be/([^?&]+)", query)
        if m:
            query = f"https://www.youtube.com/watch?v={m.group(1)}"
    try:
        result = await fetch_youtube_link(query)
    except Exception as primary_err:
        await processing_message.edit("⚠️ Primary search failed. Using backup API, this may take a few seconds…")
        try:
            result = await fetch_youtube_link_backup(query)
        except Exception as backup_err:
            await processing_message.edit(f"❌ Both search APIs failed:\nPrimary: {primary_err}\nBackup: {backup_err}")
            return
    if isinstance(result, dict) and "playlist" in result:
        playlist_items = result["playlist"]
        if not playlist_items:
            await processing_message.edit("❌ No videos found in the playlist.")
            return
        chat_containers.setdefault(chat_id, [])
        for item in playlist_items:
            secs = isodate.parse_duration(item["duration"]).total_seconds()
            if secs > MAX_DURATION_SECONDS:
                continue
            chat_containers[chat_id].append({
                "url": item["link"],
                "title": item["title"],
                "duration": iso8601_to_human_readable(item["duration"]),
                "duration_seconds": secs,
                "requester": message.from_user.first_name,
                "thumbnail": item["thumbnail"],
                'is_local': False
            })
        total = len(playlist_items)
        first_song_title = chat_containers[chat_id][-total]['title']
        reply_text = (
            f"✨ Added to queue: {total} songs from playlist.\n"
            f"**First song:** {first_song_title}"
        )
        await message.reply(reply_text)
        if len(chat_containers[chat_id]) == total:
            first_song_info = chat_containers[chat_id][0]
            await fallback_local_playback(chat_id, processing_message, first_song_info)
        else:
            await processing_message.delete()
    else:
        video_url, title, duration_iso, thumb = result
        if not video_url:
            await processing_message.edit("❌ Could not find the song. Try another query.\nSupport: @frozensupport1")
            return
        secs = isodate.parse_duration(duration_iso).total_seconds()
        if secs > MAX_DURATION_SECONDS:
            await processing_message.edit("❌ Streams longer than 2 hours are not allowed.")
            return
        readable = iso8601_to_human_readable(duration_iso)
        chat_containers.setdefault(chat_id, [])
        song_info = {
            "url": video_url,
            "title": title,
            "duration": readable,
            "duration_seconds": secs,
            "requester": message.from_user.first_name,
            "thumbnail": thumb,
            'is_local': False
        }
        chat_containers[chat_id].append(song_info)
        if len(chat_containers[chat_id]) == 1:
            await fallback_local_playback(chat_id, processing_message, song_info)
        else:
            queue_buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Skip", callback_data="skip"), InlineKeyboardButton("🗑 Clear", callback_data="clear")]
            ])
            await message.reply(
                f"✨ Added to queue :\n\n"
                f"**❍ Title ➥** {title}\n"
                f"**❍ Time ➥** {readable}\n"
                f"**❍ By ➥ ** {message.from_user.first_name}\n"
                f"**Queue number:** {len(chat_containers[chat_id])}",
                reply_markup=queue_buttons
            )
            await processing_message.delete()


@bot.on_message(filters.group & filters.command("queue"))
async def queue_handler(_, message: Message):
    chat_id = message.chat.id
    if chat_id not in chat_containers or not chat_containers[chat_id]:
        await message.reply("❌ The queue is empty.")
        return
    current_song = chat_containers[chat_id][0]
    queue_list = "\n".join([f"**{i+1}.** `{_one_line_title(song['title'])}`" for i, song in enumerate(chat_containers[chat_id])])
    text = (
        f"**🎧 Now Playing:**\n"
        f"**»** `{current_song['title']}`\n\n"
        f"**🎶 Queue ({len(chat_containers[chat_id])} songs):**\n"
        f"{queue_list}"
    )
    await message.reply(text)

@bot.on_message(filters.group & filters.command("saveplaylist"))
async def save_playlist_handler(_, message: Message):
    if not message.reply_to_message:
        await message.reply("❌ You must reply to a song to save a playlist.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("❌ Please provide a name for your playlist. Usage: `/saveplaylist <name>`")
        return
    playlist_name = parts[1].strip()
    user_id = message.from_user.id
    if user_id not in chat_containers or not chat_containers[user_id]:
        await message.reply("❌ You do not have an active queue to save.")
        return
    playlist_data = chat_containers[user_id]
    try:
        playlists_collection.update_one(
            {"user_id": user_id, "name": playlist_name},
            {"$set": {"songs": playlist_data}},
            upsert=True
        )
        await message.reply(f"✅ Playlist `{playlist_name}` saved with {len(playlist_data)} songs.")
    except Exception as e:
        await message.reply(f"❌ Failed to save playlist: {e}")

@bot.on_message(filters.group & filters.command("loadplaylist"))
async def load_playlist_handler(_, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("❌ Please provide the name of the playlist to load. Usage: `/loadplaylist <name>`")
        return
    playlist_name = parts[1].strip()
    user_id = message.from_user.id
    playlist_doc = playlists_collection.find_one({"user_id": user_id, "name": playlist_name})
    if not playlist_doc:
        await message.reply(f"❌ Playlist `{playlist_name}` not found.")
        return
    songs = playlist_doc.get("songs", [])
    if not songs:
        await message.reply(f"❌ Playlist `{playlist_name}` is empty.")
        return
    chat_id = message.chat.id
    chat_containers[chat_id] = songs
    await message.reply(f"✅ Loaded playlist `{playlist_name}` with {len(songs)} songs.\nPlaying the first song now...")
    first_song_info = chat_containers[chat_id][0]
    dummy_msg = await bot.send_message(chat_id, f"🎧 Preparing first song from playlist: **{first_song_info['title']}** ...")
    await fallback_local_playback(chat_id, dummy_msg, first_song_info)

@bot.on_message(filters.group & filters.command("myplaylists"))
async def my_playlists_handler(_, message: Message):
    user_id = message.from_user.id
    playlists = list(playlists_collection.find({"user_id": user_id}))
    if not playlists:
        await message.reply("❌ You have no saved playlists.")
        return
    playlist_list = "\n".join([f"**»** `{p['name']}` ({len(p.get('songs', []))} songs)" for p in playlists])
    await message.reply(f"**Your Saved Playlists:**\n\n{playlist_list}")

def _one_line_title(full_title: str) -> str:
    MAX_TITLE_LEN = 20
    if len(full_title) <= MAX_TITLE_LEN:
        return full_title
    else:
        return full_title[:(MAX_TITLE_LEN - 1)] + "…"

def format_time(seconds: float) -> str:
    secs = int(seconds)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    else:
        return f"{m}:{s:02d}"

def get_progress_bar_styled(elapsed: float, total: float, bar_length: int = 14) -> str:
    if total <= 0:
        return "Progress: N/A"
    fraction = min(elapsed / total, 1)
    marker_index = int(fraction * bar_length)
    if marker_index >= bar_length:
        marker_index = bar_length - 1
    left = "━" * marker_index
    right = "─" * (bar_length - marker_index - 1)
    bar = left + "❄️" + right
    return f"{format_time(elapsed)} {bar} {format_time(total)}"

async def update_progress_caption(
    chat_id: int,
    progress_message: Message,
    start_time: float,
    total_duration: float,
    base_caption: str
):
    while True:
        elapsed = time.time() - start_time
        if elapsed > total_duration:
            elapsed = total_duration
        progress_bar = get_progress_bar_styled(elapsed, total_duration)
        control_row = [
            InlineKeyboardButton(text="▷", callback_data="pause"),
            InlineKeyboardButton(text="II", callback_data="resume"),
            InlineKeyboardButton(text="‣‣I", callback_data="skip"),
            InlineKeyboardButton(text="▢", callback_data="stop")
        ]
        progress_button = InlineKeyboardButton(text=progress_bar, callback_data="progress")
        playlist_button = InlineKeyboardButton(text="➕ᴀᴅᴅ тσ ρℓαυℓιѕт➕", callback_data="add_to_playlist")
        new_keyboard = InlineKeyboardMarkup([
            control_row,
            [progress_button],
            [playlist_button]
        ])
        try:
            await bot.edit_message_caption(
                chat_id,
                progress_message.id,
                caption=base_caption,
                reply_markup=new_keyboard
            )
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                print(f"Error updating progress caption for chat {chat_id}: {e}")
            break
        if elapsed >= total_duration:
            break
        await asyncio.sleep(18)

async def fallback_local_playback(chat_id: int, message: Message, song_info: dict):
    playback_mode[chat_id] = "local"
    try:
        if chat_id in playback_tasks:
            playback_tasks[chat_id].cancel()
        try:
            await message.edit(f"Starting local playback for ⚡ {song_info['title']}...")
        except Exception:
            message = await bot.send_message(
                chat_id,
                f"Starting local playback for ⚡ {song_info['title']}..."
            )
        video_url = song_info.get("url")
        if not video_url:
            await message.edit("❌ Invalid song URL.")
            chat_containers[chat_id].pop(0)
            return
        media_path = await vector_transport_resolver(video_url)
        song_info['file_path'] = media_path
        await call_py.play(
            chat_id,
            MediaStream(media_path, video_flags=MediaStream.Flags.IGNORE)
        )
        playback_tasks[chat_id] = asyncio.current_task()
        total_duration = song_info.get("duration_seconds", 0)
        one_line = _one_line_title(song_info["title"])
        base_caption = (
            "<blockquote>"
            "<b> @bestshayri_raj  🎧 Streaming</b> (Local Playback)\n\n"
            f"❍ <b>Title:</b> {one_line}\n"
            f"❍ <b>Requested by:</b> {song_info['requester']}"
            "</blockquote>"
        )
        initial_progress = get_progress_bar_styled(0, total_duration)
        control_row = [
            InlineKeyboardButton(text="▷", callback_data="pause"),
            InlineKeyboardButton(text="II", callback_data="resume"),
            InlineKeyboardButton(text="‣‣I", callback_data="skip"),
            InlineKeyboardButton(text="▢", callback_data="stop"),
        ]
        progress_button = InlineKeyboardButton(text=initial_progress, callback_data="progress")
        playlist_button = InlineKeyboardButton(text="➕ᴀᴅᴅ тσ ρℓαυℓιѕт➕", callback_data="add_to_playlist")
        base_keyboard = InlineKeyboardMarkup([control_row, [progress_button], [playlist_button]])
        thumb_url = song_info.get("thumbnail")
        if thumb_url:
            progress_message = await message.reply_photo(
                photo=thumb_url,
                caption=base_caption,
                reply_markup=base_keyboard,
                parse_mode=ParseMode.HTML
            )
        else:
            progress_message = await message.reply(
                caption=base_caption,
                reply_markup=base_keyboard,
                parse_mode=ParseMode.HTML
            )
        try:
            await message.delete()
        except RPCError:
            pass
        asyncio.create_task(
            update_progress_caption(
                chat_id,
                progress_message,
                time.time(),
                total_duration,
                base_caption
            )
        )
        asyncio.create_task(
            bot.send_message(
                LOG_CHAT_ID,
                "#started_streaming\n"
                f"• Title: {song_info.get('title','Unknown')}\n"
                f"• Duration: {song_info.get('duration','Unknown')}\n"
                f"• Requested by: {song_info.get('requester','Unknown')}\n"
                f"• Mode: local"
            )
        )
    except Exception as e:
        print(f"Error during fallback local playback in chat {chat_id}: {e}")
        await bot.send_message(
            chat_id,
            f"❌ Failed to play “{song_info.get('title','Unknown')}” locally: {e}"
        )
        if chat_id in chat_containers and chat_containers[chat_id]:
            chat_containers[chat_id].pop(0)

@bot.on_callback_query()
async def callback_query_handler(client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    user_id = callback_query.from_user.id
    data = callback_query.data
    user = callback_query.from_user
    if not await deterministic_privilege_validator(callback_query):
        await callback_query.answer("❌ You need to be an admin to use this button.", show_alert=True)
        return
    if data == "pause":
        try:
            await call_py.pause(chat_id)
            await callback_query.answer("⏸ Playback paused.")
            await client.send_message(chat_id, f"⏸ Playback paused by {user.first_name}.")
        except Exception as e:
            await callback_query.answer("❌ Error pausing playback.", show_alert=True)
    elif data == "resume":
        try:
            await call_py.resume(chat_id)
            await callback_query.answer("▶️ Playback resumed.")
            await client.send_message(chat_id, f"▶️ Playback resumed by {user.first_name}.")
        except Exception as e:
            await callback_query.answer("❌ Error resuming playback.", show_alert=True)
    elif data == "skip":
        if chat_id not in chat_containers or not chat_containers[chat_id]:
            await callback_query.answer("❌ No songs in the queue to skip.", show_alert=True)
            return
        skipped_song = chat_containers[chat_id].pop(0)
        try:
            if skipped_song.get('file_path'):
                os.remove(skipped_song['file_path'])
        except Exception:
            pass
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        await callback_query.answer("⏩ Skipped! Playing next song...")
        await client.send_message(chat_id, f"⏩ {user.first_name} skipped **{skipped_song['title']}**.")
        if chat_id in chat_containers and chat_containers[chat_id]:
            next_song_info = chat_containers[chat_id][0]
            try:
                dummy_msg = await bot.send_message(chat_id, f"🎧 Preparing next song: **{next_song_info['title']}** ...")
                await fallback_local_playback(chat_id, dummy_msg, next_song_info)
            except Exception as e:
                print(f"Error starting next local playback: {e}")
                await bot.send_message(chat_id, f"❌ Failed to start next song: {e}")
        else:
            await safe_leave_call(chat_id)
            await client.send_message(chat_id, "❌ No more songs in the queue.")
    elif data == "clear":
        await clean_chat_resources(chat_id)
        await safe_leave_call(chat_id)
        await callback_query.answer("🗑️ Cleared the queue.")
        await callback_query.message.edit("🗑️ Cleared the queue.")
    elif data == "stop":
        await clean_chat_resources(chat_id)
        await safe_leave_call(chat_id)
        await callback_query.answer("🛑 Playback stopped and queue cleared.")
        await client.send_message(chat_id, f"🛑 Playback stopped and queue cleared by {user.first_name}.")

async def get_recommended_songs(query: str):
    try:
        url = f"https://teenage-liz-frozzennbotss-61567ab4.koyeb.app/search?title={query}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("playlist", [])
    except Exception as e:
        print(f"Failed to fetch recommendations: {e}")
        return []

@call_py.on_update(fl.stream_end())
async def stream_end_handler(_: PyTgCalls, update: StreamEnded):
    chat_id = update.chat_id
    if chat_id in chat_containers and chat_containers[chat_id]:
        skipped_song = chat_containers[chat_id].pop(0)
        try:
            if skipped_song.get('file_path'):
                os.remove(skipped_song['file_path'])
        except Exception as e:
            print(f"Error deleting file: {e}")
        if chat_id in chat_containers and chat_containers[chat_id]:
            next_song_info = chat_containers[chat_id][0]
            try:
                dummy_msg = await bot.send_message(chat_id, f"🎧 Preparing next song: **{next_song_info['title']}** ...")
                await fallback_local_playback(chat_id, dummy_msg, next_song_info)
            except Exception as e:
                print(f"Error starting next local playback: {e}")
                await bot.send_message(chat_id, f"❌ Failed to start next song: {e}")
        else:
            await safe_leave_call(chat_id)
            await bot.send_message(chat_id, "❌ No more songs in the queue.")
            await bot.send_message(chat_id, "Queue is empty. Here are some recommendations:")
            recommendations = await get_recommended_songs(skipped_song['title'])
            if recommendations:
                recommend_text = "\n".join([f"**»** `{rec['title']}`" for rec in recommendations[:5]])
                buttons = [
                    [InlineKeyboardButton(f"{i+1}", callback_data=f"recplay_{rec['link']}_{rec['title']}_{rec['duration']}_{rec['thumbnail']}") for i, rec in enumerate(recommendations[:5])]
                ]
                await bot.send_message(chat_id, f"**Top 5 Recommendations:**\n{recommend_text}", reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_message(filters.group & filters.command(["stop", "end"]))
async def stop_handler(client, message: Message):
    chat_id = message.chat.id
    if not await deterministic_privilege_validator(message):
        await message.reply("❌ You need to be an admin to use this command.")
        return
    await clean_chat_resources(chat_id)
    await safe_leave_call(chat_id)
    await message.reply("⏹ Stopped the music and cleared the queue.")

@bot.on_message(filters.group & filters.command("pause"))
async def pause_handler(client, message: Message):
    chat_id = message.chat.id
    if not await deterministic_privilege_validator(message):
        await message.reply("❌ You need to be an admin to use this command.")
        return
    try:
        await call_py.pause(chat_id)
        await message.reply("⏸ Paused the stream.")
    except Exception as e:
        await message.reply(f"❌ Failed to pause the stream.\nError: {str(e)}")

@bot.on_message(filters.group & filters.command("resume"))
async def resume_handler(client, message: Message):
    chat_id = message.chat.id
    if not await deterministic_privilege_validator(message):
        await message.reply("❌ You need to be an admin to use this command.")
        return
    try:
        await call_py.resume(chat_id)
        await message.reply("▶️ Resumed the stream.")
    except Exception as e:
        await message.reply(f"❌ Failed to resume the stream.\nError: {str(e)}")

@bot.on_message(filters.group & filters.command("skip"))
async def skip_handler(client, message: Message):
    chat_id = message.chat.id
    if not await deterministic_privilege_validator(message):
        await message.reply("❌ You need to be an admin to use this command.")
        return
    status_message = await message.reply("⏩ Skipping the current song...")
    if chat_id not in chat_containers or not chat_containers[chat_id]:
        await status_message.edit("❌ No songs in the queue to skip.")
        return
    skipped_song = chat_containers[chat_id].pop(0)
    try:
        if skipped_song.get('file_path'):
            os.remove(skipped_song['file_path'])
    except Exception as e:
        print(f"Error deleting file: {e}")
    try:
        await call_py.leave_call(chat_id)
    except Exception:
        pass
    if not chat_containers.get(chat_id):
        await status_message.edit(f"⏩ Skipped **{skipped_song['title']}**.\n\n😔 No more songs in the queue.")
        return
    next_song_info = chat_containers[chat_id][0]
    await status_message.edit(f"⏩ Skipped **{skipped_song['title']}**.\n\n💕 Playing the next song...")
    try:
        dummy_msg = await client.send_message(chat_id, f"🎧 Preparing next song: **{next_song_info['title']}** ...")
        await fallback_local_playback(chat_id, dummy_msg, next_song_info)
    except Exception as e:
        print(f"Error starting next local playback: {e}")
        await client.send_message(chat_id, f"❌ Failed to start next song: {e}")

@bot.on_message(filters.command("reboot"))
async def reboot_handler(_, message: Message):
    chat_id = message.chat.id
    await clean_chat_resources(chat_id)
    await safe_leave_call(chat_id)
    await message.reply("♻️ Rebooted for this chat. All data for this chat has been cleared.")

@bot.on_message(filters.command("ping"))
async def ping_handler(_, message: Message):
    try:
        current_time = time.time()
        uptime_seconds = int(current_time - bot_start_time)
        uptime_str = str(timedelta(seconds=uptime_seconds))
        cpu_usage = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        ram_usage = f"{memory.used // (1024 ** 2)}MB / {memory.total // (1024 ** 2)}MB ({memory.percent}%)"
        disk = psutil.disk_usage('/')
        disk_usage = f"{disk.used // (1024 ** 3)}GB / {disk.total // (1024 ** 3)}GB ({disk.percent}%)"
        response = (
            f"🏓 **Pong!**\n\n"
            f"**Local Server Stats:**\n"
            f"• **Uptime:** `{uptime_str}`\n"
            f"• **CPU Usage:** `{cpu_usage}%`\n"
            f"• **RAM Usage:** `{ram_usage}`\n"
            f"• **Disk Usage:** `{disk_usage}`"
        )
        await message.reply(response)
    except Exception as e:
        await message.reply(f"❌ Failed to execute the command.\nError: {str(e)}\n\nSupport: @bestshayri_raj")

@bot.on_message(filters.group & filters.command("clear"))
async def clear_handler(_, message: Message):
    chat_id = message.chat.id
    if not await deterministic_privilege_validator(message):
        await message.reply("❌ You need to be an admin to use this command.")
        return
    if chat_id in chat_containers:
        for song in chat_containers[chat_id]:
            try:
                if song.get('file_path'):
                    os.remove(song['file_path'])
            except Exception as e:
                print(f"Error deleting file: {e}")
        chat_containers.pop(chat_id)
        await message.reply("🗑️ Cleared the queue.")
    else:
        await message.reply("❌ No songs in the queue to clear.")

@bot.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_handler(_, message: Message):
    if not message.reply_to_message:
        await message.reply("❌ Please reply to the message you want to broadcast.")
        return
    broadcast_message = message.reply_to_message
    all_chats = list(broadcast_collection.find({}))
    success = 0
    failed = 0
    for chat in all_chats:
        try:
            target_chat_id = int(chat.get("chat_id"))
            await bot.forward_messages(
                chat_id=target_chat_id,
                from_chat_id=broadcast_message.chat.id,
                message_ids=broadcast_message.id
            )
            success += 1
        except Exception as e:
            print(f"Failed to broadcast to {target_chat_id}: {e}")
            failed += 1
        await asyncio.sleep(1)
    await message.reply(f"Broadcast complete!\n✅ Success: {success}\n❌ Failed: {failed}")

def save_state_to_db():
    data = {
        "chat_containers": { str(cid): queue for cid, queue in chat_containers.items() }
    }
    state_backup.replace_one(
        {"_id": "singleton"},
        {"_id": "singleton", "state": data},
        upsert=True
    )
    chat_containers.clear()

def load_state_from_db():
    doc = state_backup.find_one_and_delete({"_id": "singleton"})
    if not doc or "state" not in doc:
        return
    data = doc["state"]
    for cid_str, queue in data.get("chat_containers", {}).items():
        try:
            chat_containers[int(cid_str)] = queue
        except ValueError:
            continue

class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is running!")
        elif self.path == "/status":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot status: Running")
        elif self.path == "/restart":
            save_state_to_db()
            os.execl(sys.executable, sys.executable, *sys.argv)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/webhook":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                update = json.loads(body)
                asyncio.run_coroutine_threadsafe(
                    bot._process_update(update), asyncio.get_event_loop()
                )
            except Exception as e:
                print("Error processing update:", e)
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("", port), WebhookHandler)
    print(f"HTTP server running on port {port}")
    server.serve_forever()

threading.Thread(target=run_http_server, daemon=True).start()

logger = logging.getLogger(__name__)

frozen_check_ok = asyncio.Event()

async def frozen_check_handler(_, message: Message):
    if message.from_user.username == ASSISTANT_USERNAME:
        frozen_check_ok.set()
        print("Received frozen check acknowledgment.")

async def frozen_check_loop(bot_username: str):
    while True:
        try:
            frozen_check_ok.clear()
            await assistant.send_message(bot_username, "/frozen_check")
            print(f"Sent /frozen_check to @{bot_username}")
            try:
                await asyncio.wait_for(frozen_check_ok.wait(), timeout=30)
            except asyncio.TimeoutError:
                print("No frozen check reply within 30 seconds. Restarting bot.")
                await restart_bot()
        except Exception as e:
            logger.error(f"Error in frozen_check_loop: {e}")
        await asyncio.sleep(60)

async def restart_bot():
    port = int(os.environ.get("PORT", 8080))
    url = f"http://localhost:{port}/restart"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    logger.info("Local restart endpoint triggered successfully.")
                else:
                    logger.error(f"Local restart endpoint failed: {resp.status}")
    except Exception as e:
        logger.error(f"Error calling local restart endpoint: {e}")

@bot.on_callback_query(filters.regex("^recplay_"))
async def recommend_play_handler(_, callback_query: CallbackQuery):
    try:
        data_parts = callback_query.data.split('_')
        link = data_parts[1]
        title = data_parts[2]
        duration = data_parts[3]
        thumb = data_parts[4]
        song_info = {
            "url": link,
            "title": title,
            "duration": iso8601_to_human_readable(duration),
            "duration_seconds": isodate.parse_duration(duration).total_seconds(),
            "requester": callback_query.from_user.first_name,
            "thumbnail": thumb,
            'is_local': False
        }
        chat_id = callback_query.message.chat.id
        chat_containers.setdefault(chat_id, []).append(song_info)
        await callback_query.message.edit_text(f"✨ Added `{song_info['title']}` to the queue.")
        if len(chat_containers[chat_id]) == 1:
            dummy_msg = await bot.send_message(chat_id, f"🎧 Preparing recommended song: **{song_info['title']}** ...")
            await fallback_local_playback(chat_id, dummy_msg, song_info)
    except Exception as e:
        await callback_query.answer(f"❌ Error adding song: {e}", show_alert=True)

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Loading persisted state from MongoDB...")
    load_state_from_db()
    logger.info("State loaded successfully.")
    logger.info("→ Starting PyTgCalls client...")
    call_py.start()
    logger.info("PyTgCalls client started.")
    logger.info("→ Starting Telegram bot client (bot.start)...")
    try:
        bot.start()
    except Exception as e:
        logger.error(f"❌ Failed to start Pyrogram client: {e}")
        sys.exit(1)
    me = bot.get_me()
    BOT_NAME = me.first_name or "Frozen Music"
    BOT_USERNAME = me.username or os.getenv("BOT_USERNAME", "vcmusiclubot")
    BOT_LINK = f"https://t.me/{BOT_USERNAME}"
    logger.info(f"✅ Bot Name: {BOT_NAME!r}")
    logger.info(f"✅ Bot Username: {BOT_USERNAME}")
    logger.info(f"✅ Bot Link: {BOT_LINK}")
    asyncio.get_event_loop().create_task(frozen_check_loop(BOT_USERNAME))
    if not assistant.is_connected:
        logger.info("Assistant not connected; starting assistant client...")
        assistant.run()
        logger.info("Assistant client connected.")
    try:
        assistant_user = assistant.get_me()
        ASSISTANT_USERNAME = assistant_user.username
        ASSISTANT_CHAT_ID = assistant_user.id
        logger.info(f"✨ Assistant Username: {ASSISTANT_USERNAME}")
        logger.info(f"💕 Assistant Chat ID: {ASSISTANT_CHAT_ID}")
        asyncio.get_event_loop().run_until_complete(precheck_channels(assistant))
        logger.info("✅ Assistant precheck completed.")
    except Exception as e:
        logger.error(f"❌ Failed to fetch assistant info: {e}")
    logger.info("→ Entering idle() (long-polling)")
    idle()
    bot.stop()
    logger.info("Bot stopped.")
    logger.info("✅ All services are up and running. Bot started successfully.")

