import asyncio
import logging
import aiohttp
import base64
import random
import io
import json
import os 
import threading
from datetime import datetime, timedelta # <-- *** এই লাইনটি যোগ করা হয়েছে ***

from flask import Flask 

import motor.motor_asyncio

from aiogram import Bot, Dispatcher, types, F
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from typing import Callable, Dict, Any, Awaitable

from aiogram.filters import CommandStart, Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

# --- এনক্রিপশন লাইব্রেরি ---
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# --- ধাপ ১: কনফিগারেশন ---
BOT_TOKEN = os.environ.get("BOT_TOKEN") 
if not BOT_TOKEN:
    logging.critical("!!! BOT_TOKEN এনভায়রনমেন্ট ভেরিয়েবল সেট করা নেই! বট বন্ধ হয়ে যাচ্ছে।")
    exit()

ADMIN_ID = 8308179143
ADMIN_USERNAME = "Sujay_X" 

SECRET_KEY = "djchdnfkxnjhgvuy".encode('utf-8')
IV = "ayghjuiklobghfrt".encode('utf-8')

SITE_CONFIGS = {
    "diy22": {
        "name": "Diy22", "api_endpoint": "https://diy22.club/api/user/signUp",
        "api_host": "diy22.club", "origin": "https://diy22.com",
        "referer": "https://diy22.com/", "reg_host": "diy22.com"
    },
    "job777": {
        "name": "Job77", "api_endpoint": "https://job777.club/api/user/signUp",
        "api_host": "job777.club", "origin": "https://job777.com",
        "referer": "https://job777.com/", "reg_host": "job777.com"
    },
    "sms323": {
        "name": "Sms323", "api_endpoint": "https://sms323.club/api/user/signUp",
        "api_host": "sms323.club", "origin": "https://sms323.com",
        "referer": "https://sms323.com/", "reg_host": "sms323.com"
    },
    "tg377": {
        "name": "Tg377", "api_endpoint": "https://tg377.club/api/user/signUp",
        "api_host": "tg377.club", "origin": "https://tg377.vip",
        "referer": "https://tg377.vip/", "reg_host": "tg377.vip"
    }
}

# --- গ্লোবাল ভেরিয়েবল ---
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
bot = Bot(token=BOT_TOKEN)

STOP_REQUESTS = {} # {user_id: True}

# --- MongoDB সেটআপ ---
MONGO_URI = os.environ.get("MONGO_URI") 
if not MONGO_URI:
    logging.critical("!!! MONGO_URI এনভায়রনমেন্ট ভেরিয়েবল সেট করা নেই! বট বন্ধ হয়ে যাচ্ছে।")
    exit()

try:
    client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
    db = client["MyBotDatabase"] 
    approved_collection = db["approved_users"] 
    proxies_collection = db["user_proxies"] 
except Exception as e:
    logging.critical(f"MongoDB কানেক্ট করা যায়নি: {e}")
    exit()

APPROVED_USERS = {} # { user_id: expires_at_timestamp }
USER_PROXIES = {} 

# --- লগিং সেটআপ ---
logging.basicConfig(level=logging.INFO)

# --- Flask অ্যাপ (Keep Alive) ---
app = Flask(__name__)
@app.route('/')
def keep_alive():
    return "Bot is alive!"
def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# --- ধাপ ২: নতুন ডেটা লোড ফাংশন (DB থেকে) ---
async def load_data_from_db():
    global APPROVED_USERS, USER_PROXIES
    try:
        cursor = approved_collection.find({}, {"_id": 0, "user_id": 1, "expires_at": 1})
        APPROVED_USERS = {doc["user_id"]: doc.get("expires_at", 0) for doc in await cursor.to_list(None)}
        
        # অ্যাডমিনকে পার্মানেন্ট অ্যাক্সেস দেওয়া
        APPROVED_USERS[ADMIN_ID] = datetime.max.timestamp() 
        
        cursor = proxies_collection.find({})
        for doc in await cursor.to_list(None):
            USER_PROXIES[doc["user_id"]] = doc["proxy_data"]
            
        logging.info(f"✅ DB থেকে {len(APPROVED_USERS)} জন ইউজার ও {len(USER_PROXIES)} টি প্রক্সি লোড হয়েছে।")
    
    except Exception as e:
        logging.error(f"DB থেকে ডেটা লোড করায় সমস্যা: {e}")
        APPROVED_USERS = {ADMIN_ID: datetime.max.timestamp()}
        USER_PROXIES = {}

# --- অ্যাক্সেস চেক করার ফাংশন ---
def is_user_currently_approved(user_id: int) -> bool:
    if user_id not in APPROVED_USERS:
        return False
    expires_at = APPROVED_USERS.get(user_id, 0)
    return datetime.now().timestamp() < expires_at

# --- অ্যাক্সেস কন্ট্রোল Middleware ---
class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.Message | types.CallbackQuery,
        data: Dict[str, Any]
    ) -> Any:
        
        user_id = event.from_user.id
        
        if user_id == ADMIN_ID:
            return await handler(event, data)
            
        if isinstance(event, types.Message) and data.get("command") and data["command"].command == "start":
            return await handler(event, data) 
        if isinstance(event, types.CallbackQuery) and (event.data.startswith("approve:") or event.data == "cancel_fsm"):
            return await handler(event, data) 
        
        state: FSMContext = data.get('state')
        if state:
            current_state = await state.get_state()
            if current_state and current_state.startswith("UserData:getting_proxy"):
                return await handler(event, data)

        if not is_user_currently_approved(user_id):
            if user_id in APPROVED_USERS: 
                await event.answer("❌ আপনার অ্যাক্সেসের মেয়াদ শেষ হয়ে গেছে।\n"
                                   "অ্যাডমিনের সাথে যোগাযোগ করুন বা /start চেপে রিনিউ করুন।", 
                                   show_alert=True if isinstance(event, types.CallbackQuery) else False)
            else: 
                await event.answer("❌ আপনার এই বটটি ব্যবহার করার অনুমতি নেই।\n"
                                   "অনুগ্রহ করে /start চেপে অ্যাডমিনের অ্যাপ্রুভালের জন্য রিকোয়েস্ট করুন।", 
                                   show_alert=True if isinstance(event, types.CallbackQuery) else False)
            return 

        return await handler(event, data)

# --- ধাপ ৩: FSM স্টেট ---
class UserData(StatesGroup):
    getting_proxy_host = State()
    getting_proxy_port = State()
    getting_proxy_user = State()
    getting_proxy_pass = State()
    waiting_for_referral = State()
    waiting_for_amount = State()

# --- ধাপ ৪: কীবোর্ড ---
def get_user_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🚀 ACCOUNT CREATE")],
        [KeyboardButton(text="⚙️ Set/Update Proxy"), KeyboardButton(text="🔄 Change Proxy")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
def get_admin_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="📊 List Approved Users")],
        [KeyboardButton(text="🚀 ACCOUNT CREATE (Admin)")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
def get_approval_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="✅ 1H", callback_data=f"approve:{user_id}:3600"),
            InlineKeyboardButton(text="✅ 6H", callback_data=f"approve:{user_id}:21600"),
            InlineKeyboardButton(text="✅ 1D", callback_data=f"approve:{user_id}:86400"),
            InlineKeyboardButton(text="✅ 1W", callback_data=f"approve:{user_id}:604800")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
def get_stop_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="⏹️ Cancel Operation", callback_data=f"stop:{user_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
def get_fsm_cancel_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="⏹️ Cancel Operation", callback_data="cancel_fsm")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
def get_site_selection_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Diy22", callback_data="select_site:diy22")],
        [InlineKeyboardButton(text="Job77", callback_data="select_site:job777")],
        [InlineKeyboardButton(text="Sms323", callback_data="select_site:sms323")],
        [InlineKeyboardButton(text="Tg377", callback_data="select_site:tg377")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
def get_contact_admin_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📞 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ধাপ ৫: হেলপার ফাংশন ---
def encrypt_data(data_str: str) -> str:
    try:
        cipher = AES.new(SECRET_KEY, AES.MODE_CBC, IV)
        data_bytes = data_str.encode('utf-8')
        padded_data = pad(data_bytes, AES.block_size)
        encrypted_bytes = cipher.encrypt(padded_data)
        return base64.b64encode(encrypted_bytes).decode('utf-8')
    except Exception as e:
        logging.error(f"এনক্রিপশনে সমস্যা: {e}")
        return None
def generate_random_number(length: int = 10) -> str:
    return "".join(random.choices("0123456789", k=length))

# --- ধাপ ৬: API কল ---
async def call_api(encrypted_username: str, invite_code: str, proxy_url: str | None, site_config: dict) -> tuple[bool, dict]:
    payload = {
        'username': encrypted_username, 'password': '123456',
        'confirm_password': '123456', 'invite_code': invite_code,
        'reg_host': site_config['reg_host']
    }
    headers = {
        "host": site_config['api_host'], "origin": site_config['origin'],
        "referer": site_config['referer'], "accept": "application/json, text/plain, */*",
        "content-type": "application/x-www-form-urlencoded",
        "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36",
        "token": ""
    }
    if proxy_url: logging.info(f"প্রক্সি {proxy_url.split('@')[-1]} দিয়ে {site_config['name']}-এ কল করা হচ্ছে...")
    else: logging.info(f"প্রক্সি ছাড়া {site_config['name']}-এ কল করা হচ্ছে...")
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                site_config['api_endpoint'], data=payload, timeout=30, proxy=proxy_url
            ) as response:
                try: response_data = await response.json()
                except aiohttp.ContentTypeError: response_data = {"code": -1, "msg": f"JSON Error (Status: {response.status})"}
                return (True, response_data) if response.status == 200 and response_data.get('code') == 1 else (False, response_data)
    except aiohttp.ClientProxyConnectionError as e:
        logging.error(f"প্রক্সি কানেকশনে সমস্যা: {proxy_url} - {e}"); return False, {"code": -1, "msg": f"প্রক্সি এরর (আবার চেষ্টা করা হচ্ছে)"}
    except asyncio.TimeoutError:
        logging.error("API কল টাইমআউট।"); return False, {"code": -1, "msg": "সার্ভার টাইমআউট (আবার চেষ্টা করা হচ্ছে)"}
    except Exception as e:
        logging.error(f"API কল করার সময় এরর: {e}"); return False, {"code": -1, "msg": "সার্ভারের সাথে সংযোগ করা যাচ্ছে না (আবার চেষ্টা করা হচ্ছে)"}

# --- ধাপ ৭: টাস্ক প্রসেসর (ইউজার) ---
async def process_batch_task(
    user_id: int, amount: int, referral_code: str, site_config: dict, 
    proxy_host: str, proxy_port: str, proxy_user: str, proxy_pass: str,
    handler_message_id: int
):
    created_accounts = []
    user_stopped = False
    site_name = site_config['name']
    try:
        await bot.edit_message_text(
            f"✅ আপনার **{site_name}**-এর রিকোয়েস্টটি গ্রহণ করা হয়েছে এবং কাজ শুরু হচ্ছে...",
            chat_id=user_id,
            message_id=handler_message_id,
            parse_mode="Markdown",
            reply_markup=get_stop_keyboard(user_id)
        )
    except Exception as e:
        logging.error(f"User {user_id} কে মেসেজ এডিট করা যায়নি: {e}"); return
    try:
        for i in range(amount):
            if STOP_REQUESTS.get(user_id):
                user_stopped = True; del STOP_REQUESTS[user_id]; await bot.edit_message_text("⏹️ অপারেশনটি বাতিল করা হয়েছে।", chat_id=user_id, message_id=handler_message_id, reply_markup=None); break 
            username_number = generate_random_number(); encrypted_username = encrypt_data(username_number)
            if not encrypted_username:
                await bot.edit_message_text(f"❌ ({site_name}) অ্যাকাউন্ট {i+1} এনক্রিপশনে সমস্যা। স্কিপ করা হলো।", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id)); continue 
            await bot.edit_message_text(f"📊 ({site_name}) **অবস্থান:** {i+1}/{amount}\n⏳ `{username_number}` তৈরি করা হচ্ছে...", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=get_stop_keyboard(user_id))
            
            success = False; attempt = 0; retry_delays = [0, 10, 30, 60]
            while not success:
                if STOP_REQUESTS.get(user_id):
                    user_stopped = True; del STOP_REQUESTS[user_id]; await bot.edit_message_text("⏹️ অপারেশনটি বাতিল করা হয়েছে।", chat_id=user_id, message_id=handler_message_id, reply_markup=None); break 
                delay = 0
                if attempt < len(retry_delays): delay = retry_delays[attempt]
                else: delay = 60 
                if delay > 0:
                    await bot.edit_message_text(f"📊 ({site_name}) **অবস্থান:** {i+1}/{amount}\n⏳ `{username_number}` - সার্ভার ব্যাস্ত।\n⏱️ **{delay}** সেকেন্ড পর আবার চেষ্টা করা হচ্ছে... (চেষ্টা: {attempt})", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=get_stop_keyboard(user_id)); await asyncio.sleep(delay)
                await bot.edit_message_text(f"📊 ({site_name}) **অবস্থান:** {i+1}/{amount}\n⏳ `{username_number}` - API কল চলছে... (চেষ্টা: {attempt})", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=get_stop_keyboard(user_id))
                
                session_id = random.randint(100000, 999999); rotated_proxy_user = f"{proxy_user}-session-{session_id}"
                proxy_url = f"http://{rotated_proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
                
                api_success, data = await call_api(encrypted_username, referral_code, proxy_url, site_config) 
                
                if api_success: 
                    await bot.edit_message_text(f"📊 ({site_name}) **অবস্থান:** {i+1}/{amount}\n✅ `{username_number}` সফলভাবে তৈরি হয়েছে!", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=get_stop_keyboard(user_id)); created_accounts.append((username_number, "123456")); success = True 
                else: 
                    api_message = data.get('msg', 'Unknown Error').lower()
                    if "already exist" in api_message or "username already" in api_message or "invite code invalid" in api_message:
                        await bot.edit_message_text(f"📊 ({site_name}) **অবস্থান:** {i+1}/{amount}\n❌ `{username_number}` তৈরিতে ব্যর্থ: {data.get('msg', 'API Error')}", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=get_stop_keyboard(user_id)); break 
                    else: attempt += 1; continue 
            if user_stopped: break 
            await asyncio.sleep(1) 

        if not user_stopped:
            if created_accounts:
                await bot.edit_message_text(f"✅ ({site_name}) সমস্ত কাজ সম্পন্ন হয়েছে!\n🎉 মোট {len(created_accounts)} টি অ্যাকাউন্ট সফলভাবে তৈরি হয়েছে।", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=None)
            else:
                await bot.edit_message_text(f"ℹ️ ({site_name}) কাজ সম্পন্ন হয়েছে, কিন্তু কোনো অ্যাকাউন্ট তৈরি করা সম্ভব হয়নি।", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=None)
        
        if created_accounts:
            file_content = ""; [file_content := file_content + f"{user}\n{pw}\n" for user, pw in created_accounts]
            file_data = io.StringIO(file_content); file_to_send = BufferedInputFile(file_data.getvalue().encode('utf-8'), filename=f"{site_name}_accounts.txt")
            await bot.send_document(user_id, file_to_send, caption=f"{site_name}-এর জন্য আপনার তৈরি করা অ্যাকাউন্টগুলির তালিকা।")
            
    except TelegramBadRequest as e:
        if "message is not modified" in str(e): pass 
        else: logging.error(f"টেলিগ্রাম এরর: {e}")
    except Exception as e:
        logging.error(f"টাস্ক প্রসেসরে মারাত্মক সমস্যা: {e} (User: {user_id})")
    finally:
        if user_id in STOP_REQUESTS: del STOP_REQUESTS[user_id]
        
# --- টাস্ক প্রসেসর (অ্যাডমিন) ---
async def process_batch_task_admin(user_id: int, amount: int, referral_code: str, site_config: dict, handler_message_id: int):
    created_accounts = []
    user_stopped = False
    site_name = site_config['name']
    try:
        await bot.edit_message_text(f"✅ (অ্যাডমিন মোড) আপনার **{site_name}**-এর রিকোয়েস্টটি গ্রহণ করা হয়েছে এবং কাজ শুরু হচ্ছে...", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=get_stop_keyboard(user_id))
    except Exception as e:
        logging.error(f"Admin {user_id} কে মেসেজ এডিট করা যায়নি: {e}"); return
    try:
        for i in range(amount):
            if STOP_REQUESTS.get(user_id):
                user_stopped = True; del STOP_REQUESTS[user_id]; await bot.edit_message_text("⏹️ অপারেশনটি বাতিল করা হয়েছে।", chat_id=user_id, message_id=handler_message_id, reply_markup=None); break 
            username_number = generate_random_number(); encrypted_username = encrypt_data(username_number)
            if not encrypted_username:
                await bot.edit_message_text(f"❌ ({site_name}) অ্যাকাউন্ট {i+1} এনক্রিপশনে সমস্যা। স্কিপ করা হলো।", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id)); continue 
            await bot.edit_message_text(f"📊 ({site_name}) **অবস্থান:** {i+1}/{amount}\n⏳ `{username_number}` তৈরি করা হচ্ছে...", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=get_stop_keyboard(user_id))
            success = False; attempt = 0; retry_delays = [0, 10, 30, 60]
            while not success:
                if STOP_REQUESTS.get(user_id):
                    user_stopped = True; del STOP_REQUESTS[user_id]; await bot.edit_message_text("⏹️ অপারেশনটি বাতিল করা হয়েছে।", chat_id=user_id, message_id=handler_message_id, reply_markup=None); break 
                delay = 0
                if attempt < len(retry_delays): delay = retry_delays[attempt]
                else: delay = 60 
                if delay > 0:
                    await bot.edit_message_text(f"📊 ({site_name}) **অবস্থান:** {i+1}/{amount}\n⏳ `{username_number}` - সার্ভার ব্যাস্ত।\n⏱️ **{delay}** সেকেন্ড পর আবার চেষ্টা করা হচ্ছে... (চেষ্টা: {attempt})", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=get_stop_keyboard(user_id)); await asyncio.sleep(delay)
                await bot.edit_message_text(f"📊 ({site_name}) **অবস্থান:** {i+1}/{amount}\n⏳ `{username_number}` - API কল চলছে... (চেষ্টা: {attempt})", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=get_stop_keyboard(user_id))
                
                api_success, data = await call_api(encrypted_username, referral_code, None, site_config) # <-- প্রক্সি None
                
                if api_success: 
                    await bot.edit_message_text(f"📊 ({site_name}) **অবস্থান:** {i+1}/{amount}\n✅ `{username_number}` সফলভাবে তৈরি হয়েছে!", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=get_stop_keyboard(user_id)); created_accounts.append((username_number, "123456")); success = True 
                else: 
                    api_message = data.get('msg', 'Unknown Error').lower()
                    if "already exist" in api_message or "username already" in api_message or "invite code invalid" in api_message:
                        await bot.edit_message_text(f"📊 ({site_name}) **অবস্থান:** {i+1}/{amount}\n❌ `{username_number}` তৈরিতে ব্যর্থ: {data.get('msg', 'API Error')}", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=get_stop_keyboard(user_id)); break 
                    else: attempt += 1; continue 
            if user_stopped: break 
            await asyncio.sleep(1) 
        if not user_stopped:
            if created_accounts:
                await bot.edit_message_text(f"✅ ({site_name}) সমস্ত কাজ সম্পন্ন হয়েছে!\n🎉 মোট {len(created_accounts)} টি অ্যাকাউন্ট সফলভাবে তৈরি হয়েছে।", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=None)
            else:
                await bot.edit_message_text(f"ℹ️ ({site_name}) কাজ সম্পন্ন হয়েছে, কিন্তু কোনো অ্যাকাউন্ট তৈরি করা সম্ভব হয়নি।", chat_id=user_id, message_id=handler_message_id, parse_mode="Markdown", reply_markup=None)

        if created_accounts:
            file_content = ""; [file_content := file_content + f"{user}\n{pw}\n" for user, pw in created_accounts]
            file_data = io.StringIO(file_content); file_to_send = BufferedInputFile(file_data.getvalue().encode('utf-8'), filename=f"{site_name}_accounts.txt")
            await bot.send_document(user_id, file_to_send, caption=f"{site_name}-এর জন্য আপনার তৈরি করা অ্যাকাউন্টগুলির তালিকা।")
            
    except TelegramBadRequest as e:
        if "message is not modified" in str(e): pass 
        else: logging.error(f"টেলিগ্রাম এরর: {e}")
    except Exception as e:
        logging.error(f"অ্যাডমিন টাস্ক প্রসেসরে মারাত্মক সমস্যা: {e} (User: {user_id})")
    finally:
        if user_id in STOP_REQUESTS: del STOP_REQUESTS[user_id]

# --- ধাপ ৮: টেলিগ্রাম বট হ্যান্ডলার ---

@dp.message(F.text == "📊 List Approved Users")
async def list_approved_users(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    text = "👤 **Approved Users List:**\n"; text += " (No users approved yet)" if len(APPROVED_USERS) <= 1 else ""
    now = datetime.now().timestamp()
    for user_id, expires_at in APPROVED_USERS.items():
        if user_id == ADMIN_ID:
            text += f"- `{user_id}` (Admin, Permanent)\n"
            continue
        
        if expires_at > now:
            remaining_time = expires_at - now
            if remaining_time > 86400: status = f"✅ Active ({remaining_time / 86400:.1f} days left)"
            else: status = f"✅ Active ({remaining_time / 3600:.1f} hours left)"
        else:
            status = "❌ Expired"
        text += f"- `{user_id}` ({status})\n"
    await message.answer(text, parse_mode="Markdown", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data.startswith("approve:"))
async def approve_user_handler(query: types.CallbackQuery, state: FSMContext):
    if query.from_user.id != ADMIN_ID:
        await query.answer("❗️ এটি শুধুমাত্র অ্যাডমিন করতে পারে।", show_alert=True); return
    try:
        parts = query.data.split(":")
        user_id_to_approve = int(parts[1])
        duration_seconds = int(parts[2])
    except Exception as e:
        await query.answer("Error parsing callback.", show_alert=True); logging.error(f"Callback error: {e}"); return

    expires_at = datetime.now().timestamp() + duration_seconds
    duration_hours = duration_seconds / 3600
    
    await approved_collection.update_one(
        {"user_id": user_id_to_approve},
        {"$set": {"expires_at": expires_at}},
        upsert=True
    )
    APPROVED_USERS[user_id_to_approve] = expires_at
    
    await query.message.edit_text(f"✅ ইউজার {user_id_to_approve} কে {duration_hours:.0f} ঘণ্টার জন্য অ্যাপ্রুভ করা হয়েছে।", reply_markup=None)
    
    try:
        await bot.send_message(user_id_to_approve, 
                               f"🎉 অভিনন্দন! অ্যাডমিন আপনার অ্যাক্সেস {duration_hours:.0f} ঘণ্টার জন্য অ্যাপ্রুভ/রিনিউ করেছে।\n\n"
                               "বটটি ব্যবহার করতে /start চাপুন।")
    except Exception as e: 
        logging.error(f"অ্যাপ্রুভড ইউজারকে মেসেজ পাঠানো যায়নি: {e}")
    await query.answer("User approved!")

@dp.message(CommandStart())
async def send_welcome(message: types.Message, state: FSMContext):
    user_id = message.from_user.id; user_name = message.from_user.full_name
    await state.clear() 
    
    if user_id == ADMIN_ID:
        await message.answer(f"👑 স্বাগতম, অ্যাডমিন {user_name}! আপনার জন্য অ্যাডমিন প্যানেল।",
                             reply_markup=get_admin_keyboard())
        return

    # --- *** /start-এর নতুন লজিক *** ---
    if user_id not in APPROVED_USERS:
        await message.answer("👋 স্বাগতম! এই বটটি ব্যবহার করার জন্য অ্যাডমিনের অ্যাপ্রুভাল প্রয়োজন।\n"
                             "⏳ আপনার রিকোয়েস্ট অ্যাডমিনের কাছে পাঠানো হয়েছে। অনুগ্রহ করে অপেক্ষা করুন...",
                             reply_markup=get_contact_admin_keyboard())
        try:
            await bot.send_message(ADMIN_ID, f"❗️ **New User Request** ❗️\n\n"
                                   f"**Name:** {user_name}\n**User ID:** `{user_id}`\n\n"
                                   f"এই ইউজার বটটি ব্যবহার করতে চায়। আপনি কি অ্যাপ্রুভ করবেন?",
                                   parse_mode="Markdown", reply_markup=get_approval_keyboard(user_id))
        except Exception as e: logging.error(f"অ্যাডমিনকে অ্যাপ্রুভাল মেসেজ পাঠানো যায়নি: {e}")
        return

    if not is_user_currently_approved(user_id):
        await message.answer("❌ আপনার অ্যাক্সেসের মেয়াদ শেষ হয়ে গেছে।\n"
                             "⏳ আপনার রিনিউ রিকোয়েস্ট অ্যাডমিনের কাছে পাঠানো হয়েছে। অনুগ্রহ করে অপেক্ষা করুন...",
                             reply_markup=get_contact_admin_keyboard())
        try:
            await bot.send_message(ADMIN_ID, f"❗️ **User Renewal Request** ❗️\n\n"
                                   f"**Name:** {user_name}\n**User ID:** `{user_id}`\n\n"
                                   f"এই ইউজারের অ্যাক্সেস শেষ হয়ে গেছে এবং সে রিনিউ করতে চায়।",
                                   parse_mode="Markdown", reply_markup=get_approval_keyboard(user_id))
        except Exception as e: logging.error(f"অ্যাডমিনকে রিনিউ মেসেজ পাঠানো যায়নি: {e}")
        return

    if str(user_id) in USER_PROXIES:
        await message.answer(f"স্বাগতম, {user_name}! 👋\nঅ্যাকাউন্ট তৈরি করতে নিচের বাটনগুলি ব্যবহার করুন:",
                             reply_markup=get_user_keyboard())
    else:
        await message.answer(f"👋 স্বাগতম, {user_name}!\n\n"
                             "এই বটটি ব্যবহার করার জন্য প্রথমে আপনার ABC প্রক্সি সেট করতে হবে।\n\n"
                             "🔑 দয়া করে আপনার **Host** টি লিখুন:\n"
                             "(e.g., as.d3230a9b316c9763.abcproxy.vip)",
                             reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(UserData.getting_proxy_host)

@dp.callback_query(F.data.startswith("stop:"))
async def stop_creation_handler(query: types.CallbackQuery, state: FSMContext):
    try: user_id = int(query.data.split(":")[1])
    except Exception: await query.answer("Error.", show_alert=True); return
    if query.from_user.id != user_id:
        await query.answer("❗️ এটি আপনার টাস্ক নয়।", show_alert=True); return
    STOP_REQUESTS[user_id] = True
    await query.answer("... বাতিল করার রিকোয়েস্ট পাঠানো হয়েছে ..."); 
    try:
        await query.message.edit_text("⏳ অপারেশনটি বাতিল করা হচ্ছে...", reply_markup=None)
    except TelegramBadRequest: pass 

@dp.callback_query(F.data == "cancel_fsm")
async def cancel_fsm_handler(query: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await query.message.edit_text("❌ অপারেশনটি বাতিল করা হয়েছে।")
    await query.answer()

@dp.message(F.text == "⚙️ Set/Update Proxy")
async def handle_set_proxy(message: types.Message, state: FSMContext):
    if not is_user_currently_approved(message.from_user.id):
        await message.answer("❌ আপনার অ্যাক্সেসের মেয়াদ শেষ হয়ে গেছে। /start চাপুন।"); return
    await state.clear() 
    if str(message.from_user.id) in USER_PROXIES:
        await message.answer("✅ আপনার প্রক্সি ইতিমধ্যেই সেভ করা আছে।\n"
                             "যদি এটি পরিবর্তন করতে চান, '🔄 Change Proxy' বাটনে ক্লিক করুন।",
                             reply_markup=get_user_keyboard())
        return
    await message.answer("🔑 আপনার ABC প্রক্সি সেটআপ শুরু করছি।\n\n"
                         "দয়া করে **Host** টি লিখুন:\n(e.g., as.d3230a9b316c9763.abcproxy.vip)",
                         reply_markup=types.ReplyKeyboardRemove()); await state.set_state(UserData.getting_proxy_host)

@dp.message(F.text == "🔄 Change Proxy")
async def handle_change_proxy(message: types.Message, state: FSMContext):
    if not is_user_currently_approved(message.from_user.id):
        await message.answer("❌ আপনার অ্যাক্সেসের মেয়াদ শেষ হয়ে গেছে। /start চাপুন।"); return
    await state.clear() 
    await message.answer("🔑 আপনার নতুন ABC প্রক্সি সেটআপ শুরু করছি।\n\n"
                         "দয়া করে **Host** টি লিখুন:\n(e.g., as.d3230a9b316c9763.abcproxy.vip)",
                         reply_markup=types.ReplyKeyboardRemove()); await state.set_state(UserData.getting_proxy_host)

@dp.message(UserData.getting_proxy_host)
async def process_proxy_host(message: types.Message, state: FSMContext):
    await state.update_data(proxy_host=message.text)
    await message.answer("✅ Host সেভ হয়েছে।\n\nএবার **Port** টি লিখুন:\n(e.g., 4950)")
    await state.set_state(UserData.getting_proxy_port)

@dp.message(UserData.getting_proxy_port)
async def process_proxy_port(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ পোর্ট অবশ্যই একটি সংখ্যা হতে হবে। দয়া করে আবার চেষ্টা করুন।"); return
    await state.update_data(proxy_port=message.text)
    await message.answer("✅ Port সেভ হয়েছে।\n\nএবার **Username** টি লিখুন:\n(e.g., SujayJT1111-zone-abc-region-SA)")
    await state.set_state(UserData.getting_proxy_user)

@dp.message(UserData.getting_proxy_user)
async def process_proxy_user(message: types.Message, state: FSMContext):
    await state.update_data(proxy_user=message.text)
    await message.answer("✅ Username সেভ হয়েছে।\n\nএবার **Password** টি লিখুন:\n(e.g., VieMTaD5K4I)")
    await state.set_state(UserData.getting_proxy_pass)

@dp.message(UserData.getting_proxy_pass)
async def process_proxy_pass(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    proxy_info = {
        "host": user_data['proxy_host'],
        "port": user_data['proxy_port'],
        "user": user_data['proxy_user'],
        "pass": message.text 
    }
    user_id_str = str(message.from_user.id)
    USER_PROXIES[user_id_str] = proxy_info
    await proxies_collection.update_one(
        {"user_id": user_id_str},
        {"$set": {"proxy_data": proxy_info}},
        upsert=True
    )
    
    await message.answer(f"✅ **প্রক্সি সফলভাবে সেভ হয়েছে!**\n\n"
                         f"**Host:** `{proxy_info['host']}`\n**Port:** `{proxy_info['port']}`\n"
                         f"**User:** `{proxy_info['user']}`\n\n"
                         f"আপনি এখন অ্যাকাউন্ট তৈরি করতে পারেন।",
                         parse_mode="Markdown", reply_markup=get_user_keyboard()); await state.set_state(None)

@dp.message(F.text == "🚀 ACCOUNT CREATE")
@dp.message(F.text == "🚀 ACCOUNT CREATE (Admin)")
async def show_site_selection(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        if not is_user_currently_approved(message.from_user.id):
             await message.answer("❌ আপনার অ্যাক্সেসের মেয়াদ শেষ হয়ে গেছে। /start চাপুন।"); return
        if str(message.from_user.id) not in USER_PROXIES:
            await message.answer("❌ আপনি এখনও প্রক্সি সেট করেননি।\n"
                                 "দয়া করে প্রথমে '⚙️ Set/Update Proxy' বাটন চেপে আপনার প্রক্সি সেট করুন।",
                                 reply_markup=get_user_keyboard())
            return
            
    await message.answer("আপনি কোন সাইটের জন্য অ্যাকাউন্ট তৈরি করতে চান?",
                         reply_markup=get_site_selection_keyboard())

@dp.callback_query(F.data.startswith("select_site:"))
async def start_creation_process(query: types.CallbackQuery, state: FSMContext):
    if not is_user_currently_approved(query.from_user.id):
        await query.answer("❌ আপনার অ্যাক্সেসের মেয়াদ শেষ হয়ে গেছে। /start চাপুন।", show_alert=True); return
        
    site_key = query.data.split(":")[-1]
    if site_key not in SITE_CONFIGS:
        await query.answer("❌ অবৈধ সাইট।", show_alert=True); return
    
    if query.from_user.id != ADMIN_ID:
        if str(query.from_user.id) not in USER_PROXIES:
            await query.message.answer("❌ আপনি এখনও প্রক্সি সেট করেননি।\n"
                                       "দয়া করে প্রথমে '⚙️ Set/Update Proxy' বাটন চেপে আপনার প্রক্সি সেট করুন।")
            await query.answer(); return

    await state.update_data(selected_site=site_key)
    
    handler_msg = await query.message.answer(
        f"🔑 ({SITE_CONFIGS[site_key]['name']}) দয়া করে আপনার রেফার কোডটি টাইপ করুন:", 
        reply_markup=get_fsm_cancel_keyboard()
    )
    await state.update_data(handler_message_id=handler_msg.message_id)
    await state.set_state(UserData.waiting_for_referral)
    await query.answer()

@dp.message(UserData.waiting_for_referral)
async def process_referral(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    handler_msg_id = user_data.get("handler_message_id")
    site_key = user_data.get("selected_site", "diy22")
    site_name = SITE_CONFIGS.get(site_key, {}).get("name", "")
    
    if not handler_msg_id:
        await state.clear(); await message.answer("একটি সমস্যা হয়েছে, /start দিন।"); return

    await state.update_data(referral=message.text)
    
    try:
        await bot.edit_message_text(
            f"📈 ({site_name}) এখন আপনি কতগুলি অ্যাকাউন্ট তৈরি করতে চান? (সর্বোচ্চ 20 টি)",
            chat_id=message.chat.id,
            message_id=handler_msg_id,
            reply_markup=get_fsm_cancel_keyboard()
        )
        await state.set_state(UserData.waiting_for_amount)
    except Exception as e:
        logging.error(f"FSM (referral) এডিট করায় সমস্যা: {e}")
    finally:
        await message.delete() 

@dp.message(UserData.waiting_for_amount)
async def process_amount_and_queue(message: types.Message, state: FSMContext):
    try:
        amount = int(message.text)
        if not (0 < amount <= 20):
            await message.answer("❌ সর্বোচ্চ **20** টি অ্যাকাউন্ট একসাথে তৈরি করা যাবে।\n"
                                 "দয়া করে 20 বা তার কম একটি সংখ্যা দিন।")
            await message.delete(); return
        
        user_data = await state.get_data(); referral_code = user_data.get('referral'); site_key = user_data.get('selected_site')
        handler_msg_id = user_data.get("handler_message_id")

        if not all([handler_msg_id, referral_code, site_key]):
             await state.clear()
             await bot.edit_message_text("❌ একটি ত্রুটি ঘটেছে (Ref/SiteKey)। দয়া করে /start দিয়ে আবার চেষ্টা করুন।", chat_id=message.chat.id, message_id=handler_msg_id)
             await message.delete(); return
        
        site_config = SITE_CONFIGS[site_key]
        
        if message.from_user.id == ADMIN_ID:
            asyncio.create_task(
                process_batch_task_admin(message.from_user.id, amount, referral_code, site_config, handler_msg_id)
            )
        else:
            try:
                proxy_data = USER_PROXIES[str(message.from_user.id)]
                proxy_host = proxy_data['host']; proxy_port = proxy_data['port']
                proxy_user = proxy_data['user']; proxy_pass = proxy_data['pass']
            except KeyError:
                 await bot.edit_message_text("❌ আপনার প্রক্সি সেভ করা নেই। দয়া করে 'Set/Update Proxy' দিয়ে আবার সেট করুন।", chat_id=message.chat.id, message_id=handler_msg_id)
                 await state.clear(); await message.delete(); return
            
            asyncio.create_task(
                process_batch_task(message.from_user.id, amount, referral_code, site_config, 
                                   proxy_host, proxy_port, proxy_user, proxy_pass, handler_msg_id)
            )
        
        await state.clear() 
        await message.delete() 
        
    except ValueError:
        await message.answer("❌ এটি একটি সংখ্যা নয়। দয়া করে শুধুমাত্র সংখ্যা লিখুন।")
        await message.delete() 
    except Exception as e:
        await message.answer(f"একটি ত্রুটি ঘটেছে: {e}"); await state.clear()

# --- ধাপ ৯: বট চালু করা ---
async def main():
    """বট চালু করে"""
    await load_data_from_db() # <-- DB থেকে সব ডেটা লোড করা
    
    # --- *** Middleware টি সরিয়ে ফেলা হয়েছে *** ---
    
    try:
        await bot.send_message(ADMIN_ID, f"✅ বট রিস্টার্ট/চালু হয়েছে! ({len(APPROVED_USERS)} জন ইউজার অ্যাপ্রুভড, {len(USER_PROXIES)} টি প্রক্সি লোডেড)")
    except Exception as e:
        logging.warning(f"অ্যাডমিনকে ({ADMIN_ID}) মেসেজ পাঠানো যায়নি: {e}")
    
    # --- Flask সার্ভারটি থ্রেডে চালু করা ---
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    
    logging.info("বট পোলিং শুরু করছে..."); 
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
