import asyncio
import logging
import aiohttp
import base64
import random
import io
import json
import os 
import threading
from datetime import datetime, timedelta

from flask import Flask 

import motor.motor_asyncio

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.default import DefaultBotProperties

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

# --- গ্লোবাল ভেরিয়েবল ---
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))

STOP_REQUESTS = {} # {user_id: True}

# --- MongoDB সেটআপ ---
MONGO_URI = os.environ.get("MONGO_URI") 
if not MONGO_URI:
    logging.critical("!!! MONGO_URI এনভায়রনমেন্ট ভেরিয়েবল সেট করা নেই! বট বন্ধ হয়ে যাচ্ছে।")
    exit()

try:
    client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
    db = client["MyBotDatabase"] 
    users_collection = db["users_main"]
    sites_collection = db["sites"]
    config_collection = db["bot_config"]
    proxies_collection = db["user_proxies"] # <-- প্রক্সি কালেকশন
except Exception as e:
    logging.critical(f"MongoDB কানেক্ট করা যায়নি: {e}")
    exit()

USER_DATA = {} 
SITE_CONFIGS = {}
BOT_CONFIG = {} 
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
    global USER_DATA, SITE_CONFIGS, BOT_CONFIG, USER_PROXIES
    try:
        # --- ইউজার ডেটা লোড করা ---
        cursor = users_collection.find({})
        async for doc in cursor:
            USER_DATA[doc["user_id"]] = doc
        
        # --- প্রক্সি ডেটা লোড করা এবং USER_DATA-তে মার্জ করা ---
        cursor_proxy = proxies_collection.find({})
        
        # --- *** এই লুপটি ঠিক করা হয়েছে *** ---
        proxy_list = await cursor_proxy.to_list(None) 
        for doc in proxy_list: # <-- 'async for' থেকে 'async' সরানো হয়েছে
            user_id = doc["user_id"]
            if user_id not in USER_DATA:
                USER_DATA[user_id] = {"user_id": user_id, "role": "user", "expires_at": 0, "banned": False}
            USER_DATA[user_id]["proxy"] = doc["proxy_data"]
            USER_PROXIES[str(user_id)] = doc["proxy_data"] 

        # অ্যাডমিনকে পার্মানেন্ট অ্যাক্সেস দেওয়া
        if ADMIN_ID not in USER_DATA:
            admin_data = {
                "user_id": ADMIN_ID,
                "role": "admin",
                "expires_at": datetime.max.timestamp(),
                "banned": False,
                "proxy": None
            }
            await users_collection.insert_one(admin_data)
            USER_DATA[ADMIN_ID] = admin_data
        else:
            USER_DATA[ADMIN_ID]["role"] = "admin"
            USER_DATA[ADMIN_ID]["expires_at"] = datetime.max.timestamp()
        
        # --- সাইট কনফিগ লোড করা ---
        cursor_sites = sites_collection.find({})
        async for doc in cursor_sites: 
            SITE_CONFIGS[doc["site_key"]] = doc
        
        if not SITE_CONFIGS: # যদি কোনো সাইট না থাকে, ডিফল্টগুলি অ্যাড করা
            default_sites = {
                "diy22": {"name": "Diy22", "api_endpoint": "https://diy22.club/api/user/signUp", "api_host": "diy22.club", "origin": "https://diy22.com", "referer": "https://diy22.com/", "reg_host": "diy22.com"},
                "job777": {"name": "Job77", "api_endpoint": "https://job777.club/api/user/signUp", "api_host": "job777.club", "origin": "https://job777.com", "referer": "https://job777.com/", "reg_host": "job777.com"},
                "sms323": {"name": "Sms323", "api_endpoint": "https://sms323.club/api/user/signUp", "api_host": "sms323.club", "origin": "https://sms323.com", "referer": "https://sms323.com/", "reg_host": "sms323.com"},
                "tg377": {"name": "Tg377", "api_endpoint": "https://tg377.club/api/user/signUp", "api_host": "tg377.club", "origin": "https://tg377.vip", "referer": "https://tg377.vip/", "reg_host": "tg377.vip"}
            }
            for key, config in default_sites.items():
                config_with_key = config.copy()
                config_with_key["site_key"] = key
                await sites_collection.insert_one(config_with_key)
                SITE_CONFIGS[key] = config_with_key
        
        # --- বট কনফিগ লোড করা (গ্রুপ আইডি) ---
        bot_conf = await config_collection.find_one({"_id": "main_config"})
        if not bot_conf:
            BOT_CONFIG = {"group_id": None, "group_link": None}
            await config_collection.insert_one({"_id": "main_config", **BOT_CONFIG})
        else:
            BOT_CONFIG = bot_conf

        logging.info(f"✅ DB থেকে {len(USER_DATA)} জন ইউজার, {len(SITE_CONFIGS)} টি সাইট, এবং গ্রুপ কনফিগ লোড হয়েছে।")
    
    except Exception as e:
        logging.critical(f"DB থেকে ডেটা লোড করায় মারাত্মক সমস্যা: {e}")
        USER_DATA = {ADMIN_ID: {"role": "admin", "expires_at": datetime.max.timestamp(), "banned": False, "proxy": None}}
        SITE_CONFIGS = {}
        BOT_CONFIG = {"group_id": None, "group_link": None}


# --- অ্যাক্সেস চেক করার ফাংশন ---
def get_user_status(user_id: int) -> dict:
    user_doc = USER_DATA.get(user_id)
    
    if not user_doc:
        return {"status": "new"}
    if user_doc.get("banned", False):
        return {"status": "banned"}
    if user_doc.get("role") == "admin":
        return {"status": "active", "role": "admin"}
    expires_at = user_doc.get("expires_at", 0)
    if datetime.now().timestamp() < expires_at:
        return {"status": "active", "role": "user"}
    else:
        return {"status": "expired"}

# --- ধাপ ৩: FSM স্টেট ---
class UserData(StatesGroup):
    getting_proxy_host = State()
    getting_proxy_port = State()
    getting_proxy_user = State()
    getting_proxy_pass = State()
    waiting_for_referral = State()
    waiting_for_amount = State()
    adding_site_key = State()
    adding_site_name = State()
    adding_site_endpoint = State()
    adding_site_host = State()
    adding_site_origin = State()
    adding_site_referer = State()
    adding_site_reghost = State()
    removing_site_key = State()
    banning_user_id = State()
    unbanning_user_id = State()
    setting_group_id = State()
    setting_group_link = State()

# --- ধাপ ৪: কীবোর্ড ---
def get_user_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🚀 ACCOUNT CREATE")],
        [KeyboardButton(text="⚙️ Set/Update Proxy"), KeyboardButton(text="🔄 Change Proxy")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, input_field_placeholder="Select an option...")

def get_admin_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🚀 ACCOUNT CREATE (Admin)")],
        [KeyboardButton(text="📊 User List")],
        [KeyboardButton(text="🚫 User Ban Mgt")],
        [KeyboardButton(text="🌐 Site Mgt"), KeyboardButton(text="🔗 Group Mgt")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
    
def get_approval_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="✅ 30m", callback_data=f"approve:{user_id}:1800"),
            InlineKeyboardButton(text="✅ 1H", callback_data=f"approve:{user_id}:3600"),
            InlineKeyboardButton(text="✅ 6H", callback_data=f"approve:{user_id}:21600"),
        ],
        [
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
    buttons = []
    for key, config in SITE_CONFIGS.items():
        buttons.append([InlineKeyboardButton(text=config["name"], callback_data=f"select_site:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
    
def get_contact_admin_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📞 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_join_verify_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    if BOT_CONFIG.get("group_link"):
        buttons.append([InlineKeyboardButton(text="➡️ Join Group ⬅️", url=BOT_CONFIG["group_link"])])
    buttons.append([InlineKeyboardButton(text="✅ Verify", callback_data="verify_join")])
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
            f"✅ আপনার <b>{site_name}</b>-এর রিকোয়েস্টটি গ্রহণ করা হয়েছে এবং কাজ শুরু হচ্ছে...",
            chat_id=user_id,
            message_id=handler_message_id,
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
            await bot.edit_message_text(f"📊 ({site_name}) <b>অবস্থান:</b> {i+1}/{amount}\n⏳ <code>{username_number}</code> তৈরি করা হচ্ছে...", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id))
            
            success = False; attempt = 0; retry_delays = [0, 10, 30, 60]
            while not success:
                if STOP_REQUESTS.get(user_id):
                    user_stopped = True; del STOP_REQUESTS[user_id]; await bot.edit_message_text("⏹️ অপারেশনটি বাতিল করা হয়েছে।", chat_id=user_id, message_id=handler_message_id, reply_markup=None); break 
                delay = 0
                if attempt < len(retry_delays): delay = retry_delays[attempt]
                else: delay = 60 
                if delay > 0:
                    await bot.edit_message_text(f"📊 ({site_name}) <b>অবস্থান:</b> {i+1}/{amount}\n⏳ <code>{username_number}</code> - সার্ভার ব্যাস্ত।\n⏱️ <b>{delay}</b> সেকেন্ড পর আবার চেষ্টা করা হচ্ছে... (চেষ্টা: {attempt})", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id)); await asyncio.sleep(delay)
                await bot.edit_message_text(f"📊 ({site_name}) <b>অবস্থান:</b> {i+1}/{amount}\n⏳ <code>{username_number}</code> - API কল চলছে... (চেষ্টা: {attempt})", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id))
                
                session_id = random.randint(100000, 999999); rotated_proxy_user = f"{proxy_user}-session-{session_id}"
                proxy_url = f"http://{rotated_proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
                
                api_success, data = await call_api(encrypted_username, referral_code, proxy_url, site_config) 
                
                if api_success: 
                    await bot.edit_message_text(f"📊 ({site_name}) <b>অবস্থান:</b> {i+1}/{amount}\n✅ <code>{username_number}</code> সফলভাবে তৈরি হয়েছে!", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id)); created_accounts.append((username_number, "123456")); success = True 
                else: 
                    api_message = data.get('msg', 'Unknown Error').lower()
                    if "already exist" in api_message or "username already" in api_message or "invite code invalid" in api_message:
                        await bot.edit_message_text(f"📊 ({site_name}) <b>অবস্থান:</b> {i+1}/{amount}\n❌ <code>{username_number}</code> তৈরিতে ব্যর্থ: {data.get('msg', 'API Error')}", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id)); break 
                    else: attempt += 1; continue 
            if user_stopped: break 
            await asyncio.sleep(1) 

        if not user_stopped:
            if created_accounts:
                await bot.edit_message_text(f"✅ ({site_name}) সমস্ত কাজ সম্পন্ন হয়েছে!\n🎉 মোট {len(created_accounts)} টি অ্যাকাউন্ট সফলভাবে তৈরি হয়েছে।", chat_id=user_id, message_id=handler_message_id, reply_markup=None)
            else:
                await bot.edit_message_text(f"ℹ️ ({site_name}) কাজ সম্পন্ন হয়েছে, কিন্তু কোনো অ্যাকাউন্ট তৈরি করা সম্ভব হয়নি।", chat_id=user_id, message_id=handler_message_id, reply_markup=None)
        
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
        await bot.edit_message_text(f"✅ (অ্যাডমিন মোড) আপনার <b>{site_name}</b>-এর রিকোয়েস্টটি গ্রহণ করা হয়েছে এবং কাজ শুরু হচ্ছে...", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id))
    except Exception as e:
        logging.error(f"Admin {user_id} কে মেসেজ এডিট করা যায়নি: {e}"); return
    try:
        for i in range(amount):
            if STOP_REQUESTS.get(user_id):
                user_stopped = True; del STOP_REQUESTS[user_id]; await bot.edit_message_text("⏹️ অপারেশনটি বাতিল করা হয়েছে।", chat_id=user_id, message_id=handler_message_id, reply_markup=None); break 
            username_number = generate_random_number(); encrypted_username = encrypt_data(username_number)
            if not encrypted_username:
                await bot.edit_message_text(f"❌ ({site_name}) অ্যাকাউন্ট {i+1} এনক্রিপশনে সমস্যা। স্কিপ করা হলো।", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id)); continue 
            await bot.edit_message_text(f"📊 ({site_name}) <b>অবস্থান:</b> {i+1}/{amount}\n⏳ <code>{username_number}</code> তৈরি করা হচ্ছে...", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id))
            success = False; attempt = 0; retry_delays = [0, 10, 30, 60]
            while not success:
                if STOP_REQUESTS.get(user_id):
                    user_stopped = True; del STOP_REQUESTS[user_id]; await bot.edit_message_text("⏹️ অপারেশনটি বাতিল করা হয়েছে।", chat_id=user_id, message_id=handler_message_id, reply_markup=None); break 
                delay = 0
                if attempt < len(retry_delays): delay = retry_delays[attempt]
                else: delay = 60 
                if delay > 0:
                    await bot.edit_message_text(f"📊 ({site_name}) <b>অবস্থান:</b> {i+1}/{amount}\n⏳ <code>{username_number}</code> - সার্ভার ব্যাস্ত।\n⏱️ <b>{delay}</b> সেকেন্ড পর আবার চেষ্টা করা হচ্ছে... (চেষ্টা: {attempt})", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id)); await asyncio.sleep(delay)
                await bot.edit_message_text(f"📊 ({site_name}) <b>অবস্থান:</b> {i+1}/{amount}\n⏳ <code>{username_number}</code> - API কল চলছে... (চেষ্টা: {attempt})", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id))
                
                api_success, data = await call_api(encrypted_username, referral_code, None, site_config) # <-- প্রক্সি None
                
                if api_success: 
                    await bot.edit_message_text(f"📊 ({site_name}) <b>অবস্থান:</b> {i+1}/{amount}\n✅ <code>{username_number}</code> সফলভাবে তৈরি হয়েছে!", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id)); created_accounts.append((username_number, "123456")); success = True 
                else: 
                    api_message = data.get('msg', 'Unknown Error').lower()
                    if "already exist" in api_message or "username already" in api_message or "invite code invalid" in api_message:
                        await bot.edit_message_text(f"📊 ({site_name}) <b>অবস্থান:</b> {i+1}/{amount}\n❌ <code>{username_number}</code> তৈরিতে ব্যর্থ: {data.get('msg', 'API Error')}", chat_id=user_id, message_id=handler_message_id, reply_markup=get_stop_keyboard(user_id)); break 
                    else: attempt += 1; continue 
            if user_stopped: break 
            await asyncio.sleep(1) 
        if not user_stopped:
            if created_accounts:
                await bot.edit_message_text(f"✅ ({site_name}) সমস্ত কাজ সম্পন্ন হয়েছে!\n🎉 মোট {len(created_accounts)} টি অ্যাকাউন্ট সফলভাবে তৈরি হয়েছে।", chat_id=user_id, message_id=handler_message_id, reply_markup=None)
            else:
                await bot.edit_message_text(f"ℹ️ ({site_name}) কাজ সম্পন্ন হয়েছে, কিন্তু কোনো অ্যাকাউন্ট তৈরি করা সম্ভব হয়নি।", chat_id=user_id, message_id=handler_message_id, reply_markup=None)

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

@dp.message(F.text == "📊 User List")
async def list_approved_users(message: types.Message, state: FSMContext):
    if get_user_status(message.from_user.id).get("role") != "admin": return
    await state.clear()
    text_lines = ["👤 <b>User Access List:</b>\n"]
    if len(USER_DATA) <= 1: 
        text_lines.append(" (No users yet)")
    
    now = datetime.now().timestamp()
    
    sorted_users = sorted(USER_DATA.items(), key=lambda item: item[1].get('role', 'user'))

    for user_id, data in sorted_users:
        role = data.get("role", "user")
        banned = data.get("banned", False)
        expires_at = data.get("expires_at", 0)
        
        if banned:
            status = "🚫 Banned"
        elif user_id == ADMIN_ID:
            status = "👑 Admin (Permanent)"
        elif role == "sub-admin": 
            status = "🛡️ Sub-Admin (Legacy)"
        elif expires_at > now:
            remaining_time = expires_at - now
            if remaining_time > 86400: status = f"✅ Active ({remaining_time / 86400:.1f} days left)"
            else: status = f"✅ Active ({remaining_time / 3600:.1f} hours left)"
        else:
            status = "❌ Expired"
            
        text_lines.append(f"- <code>{user_id}</code> ({status})")

    await message.answer("\n".join(text_lines), reply_markup=get_admin_keyboard())

@dp.callback_query(F.data.startswith("approve:"))
async def approve_user_handler(query: types.CallbackQuery, state: FSMContext):
    if get_user_status(query.from_user.id).get("role") != "admin":
        await query.answer("❗️ এটি শুধুমাত্র অ্যাডমিন করতে পারে।", show_alert=True); return
        
    try:
        parts = query.data.split(":")
        user_id_to_approve = int(parts[1])
        duration_seconds = int(parts[2])
    except Exception as e:
        await query.answer("Error parsing callback.", show_alert=True); logging.error(f"Callback error: {e}"); return

    expires_at = datetime.now().timestamp() + duration_seconds
    
    user_data = USER_DATA.get(user_id_to_approve, {
        "user_id": user_id_to_approve,
        "role": "user",
        "banned": False,
        "proxy": None
    })
    
    user_data["expires_at"] = expires_at
    user_data["role"] = "user"
    
    await users_collection.update_one(
        {"user_id": user_id_to_approve},
        {"$set": user_data},
        upsert=True
    )
    USER_DATA[user_id_to_approve] = user_data
    
    duration_text = ""
    if duration_seconds == 1800: duration_text = "30 মিনিট"
    elif duration_seconds == 3600: duration_text = "1 ঘণ্টা"
    elif duration_seconds == 21600: duration_text = "6 ঘণ্টা"
    elif duration_seconds == 86400: duration_text = "1 দিন"
    elif duration_seconds == 604800: duration_text = "1 সপ্তাহ"
    
    await query.message.edit_text(f"✅ ইউজার {user_id_to_approve} কে {duration_text}-এর জন্য অ্যাপ্রুভ করা হয়েছে।", reply_markup=None)
    
    try:
        await bot.send_message(user_id_to_approve, 
                               f"🎉 অভিনন্দন! অ্যাডমিন আপনার অ্যাক্সেস {duration_text}-এর জন্য অ্যাপ্রুভ/রিনিউ করেছে।\n\n"
                               "বটটি ব্যবহার করতে /start চাপুন।")
    except Exception as e: 
        logging.error(f"অ্যাপ্রুভড ইউজারকে মেসেজ পাঠানো যায়নি: {e}")
    await query.answer("User approved!")

@dp.message(CommandStart())
async def send_welcome(message: types.Message, state: FSMContext):
    user_id = message.from_user.id; user_name = message.from_user.full_name
    await state.clear() 
    
    status_info = get_user_status(user_id)
    status = status_info.get("status")
    
    if status == "banned":
        await message.answer("❌ আপনি এই বটটি ব্যবহার করা থেকে ব্যানড।\nঅ্যাডমিনের সাথে যোগাযোগ করুন।", 
                             reply_markup=get_contact_admin_keyboard())
        return

    if status == "active" and status_info.get("role") == "admin":
        await message.answer(f"👑 স্বাগতম, অ্যাডমিন {user_name}! আপনার জন্য অ্যাডমিন প্যানেল।",
                             reply_markup=get_admin_keyboard())
        return

    # --- গ্রুপ জয়েন চেক ---
    group_id = BOT_CONFIG.get("group_id")
    if group_id:
        try:
            member = await bot.get_chat_member(chat_id=group_id, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                await message.answer("👋 স্বাগতম! এই বটটি ব্যবহার করার জন্য, অনুগ্রহ করে প্রথমে আমাদের গ্রুপে জয়েন করুন এবং তারপর 'Verify' বাটনে ক্লিক করুন।",
                                     reply_markup=get_join_verify_keyboard())
                return
        except (TelegramForbiddenError, TelegramBadRequest):
             logging.error(f"গ্রুপ {group_id} চেক করা যায়নি। বট কি গ্রুপের অ্যাডমিন?")
        except Exception as e:
            logging.error(f"গ্রুপ মেম্বার চেক করায় সমস্যা: {e}")

    # --- প্রাইস লিস্ট টেক্সট ---
    PRICE_LIST_TEXT = (
        "\n\n💎 <b>Unlimited Account Create Bot — Access Price</b>\n\n"
        "⏱ 30 Minute — 20 টাকা\n"
        "⏱ 1 Hour — 40 টাকা\n"
        "⏱ 6 Hours — 150 টাকা\n"
        "⏱ 1 Day — 300 টাকা\n"
        "⏱ 1 Week — 600 টাকা"
    )

    if status == "new" or status == "expired":
        msg_text = "👋 স্বাগতম!" if status == "new" else "❌ আপনার অ্যাক্সেসের মেয়াদ শেষ হয়ে গেছে।"
        await message.answer(f"{msg_text}\n⏳ আপনার রিকোয়েস্ট অ্যাডমিন প্যানেলে পাঠানো হয়েছে। অনুগ্রহ করে অপেক্ষা করুন...{PRICE_LIST_TEXT}",
                             reply_markup=get_contact_admin_keyboard())
        
        try:
            request_type = "New User Request" if status == "new" else "User Renewal Request"
            await bot.send_message(ADMIN_ID, f"❗️ <b>{request_type}</b> ❗️\n\n"
                                   f"<b>Name:</b> {message.from_user.full_name}\n<b>User ID:</b> <code>{user_id}</code>\n\n"
                                   f"এই ইউজার বটটি ব্যবহার করতে চায়। আপনি কি অ্যাপ্রুভ করবেন?",
                                   reply_markup=get_approval_keyboard(user_id))
        except Exception as e: 
            logging.error(f"অ্যাডমিন {ADMIN_ID} কে নোটিফিকেশন পাঠানো যায়নি: {e}")
        return

    # কেস: ইউজার অ্যাক্টিভ কিন্তু প্রক্সি সেট করা নেই
    if not USER_DATA.get(user_id, {}).get("proxy"):
        await message.answer(f"👋 স্বাগতম, {user_name}!\n\n"
                             "এই বটটি ব্যবহার করার জন্য প্রথমে আপনার ABC প্রক্সি সেট করতে হবে।\n\n"
                             "🔑 দয়া করে আপনার <b>Host</b> টি লিখুন:\n"
                             "(e.g., as.d3230a9b316c9763.abcproxy.vip)",
                             reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(UserData.getting_proxy_host)
        return

    # কেস: ইউজার অ্যাক্টিভ এবং প্রক্সি সেট করা আছে
    await message.answer(f"স্বাগতম, {user_name}! 👋\nঅ্যাকাউন্ট তৈরি করতে নিচের বাটনগুলি ব্যবহার করুন:",
                         reply_markup=get_user_keyboard())

@dp.callback_query(F.data == "verify_join")
async def verify_join_handler(query: types.CallbackQuery, state: FSMContext):
    group_id = BOT_CONFIG.get("group_id")
    if not group_id:
        await query.answer("গ্রুপ সেটআপ করা হয়নি।", show_alert=True); return

    try:
        member = await bot.get_chat_member(chat_id=group_id, user_id=query.from_user.id)
        if member.status not in ["member", "administrator", "creator"]:
            await query.answer("❌ আপনি এখনও গ্রুপে জয়েন করেননি। অনুগ্রহ করে জয়েন করে আবার চেষ্টা করুন।", show_alert=True)
            return
    except Exception as e:
        await query.answer("❌ ভেরিফাই করার সময় সমস্যা হয়েছে। অ্যাডমিনের সাথে যোগাযোগ করুন।", show_alert=True)
        logging.error(f"ভেরিফাই করার সময় এরর: {e}"); return
    
    await query.message.delete()
    await query.answer("✅ ভেরিফিকেশন সফল!")
    await send_welcome(query.message, state) # <-- /start ফ্লো আবার চালানো


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
    try:
        await query.message.edit_text("❌ অপারেশনটি বাতিল করা হয়েছে।")
    except TelegramBadRequest as e:
        if "message to edit not found" in str(e):
            await query.message.answer("❌ অপারেশনটি বাতিল করা হয়েছে।")
        else: raise e
    await query.answer()

@dp.message(F.text == "⚙️ Set/Update Proxy")
async def handle_set_proxy(message: types.Message, state: FSMContext):
    if not is_user_currently_approved(message.from_user.id):
        await message.answer("❌ আপনার অ্যাক্সেসের মেয়াদ শেষ হয়ে গেছে। /start চাপুন।"); return
    await state.clear() 
    if USER_DATA.get(message.from_user.id, {}).get("proxy"):
        await message.answer("✅ আপনার প্রক্সি ইতিমধ্যেই সেভ করা আছে।\n"
                             "যদি এটি পরিবর্তন করতে চান, '🔄 Change Proxy' বাটনে ক্লিক করুন।",
                             reply_markup=get_user_keyboard())
        return
    await message.answer("🔑 আপনার ABC প্রক্সি সেটআপ শুরু করছি।\n\n"
                         "দয়া করে <b>Host</b> টি লিখুন:\n(e.g., as.d3230a9b316c9763.abcproxy.vip)",
                         reply_markup=types.ReplyKeyboardRemove()); await state.set_state(UserData.getting_proxy_host)

@dp.message(F.text == "🔄 Change Proxy")
async def handle_change_proxy(message: types.Message, state: FSMContext):
    if not is_user_currently_approved(message.from_user.id):
        await message.answer("❌ আপনার অ্যাক্সেসের মেয়াদ শেষ হয়ে গেছে। /start চাপুন।"); return
    await state.clear() 
    await message.answer("🔑 আপনার নতুন ABC প্রক্সি সেটআপ শুরু করছি।\n\n"
                         "দয়া করে <b>Host</b> টি লিখুন:\n(e.g., as.d3230a9b316c9763.abcproxy.vip)",
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
    user_data_state = await state.get_data()
    proxy_info = {
        "host": user_data_state.get('proxy_host'),
        "port": user_data_state.get('proxy_port'),
        "user": user_data_state.get('proxy_user'),
        "pass": message.text 
    }
    user_id = message.from_user.id
    
    USER_DATA.setdefault(user_id, {})["proxy"] = proxy_info
    
    await proxies_collection.update_one(
        {"user_id": user_id},
        {"$set": {"proxy_data": proxy_info}},
        upsert=True
    )
    
    await message.answer(f"✅ **প্রক্সি সফলভাবে সেভ হয়েছে!**\n\n"
                         f"<b>Host:</b> <code>{proxy_info['host']}</code>\n<b>Port:</b> <code>{proxy_info['port']}</code>\n"
                         f"<b>User:</b> <code>{proxy_info['user']}</code>\n\n"
                         f"আপনি এখন অ্যাকাউন্ট তৈরি করতে পারেন।",
                         reply_markup=get_user_keyboard()); await state.set_state(None)

@dp.message(F.text == "🚀 ACCOUNT CREATE")
@dp.message(F.text == "🚀 ACCOUNT CREATE (Admin)")
async def show_site_selection(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    status_info = get_user_status(user_id)
    
    if status_info.get("status") != "active":
        await message.answer("❌ আপনার এই বটটি ব্যবহার করার অনুমতি নেই বা মেয়াদ শেষ হয়ে গেছে। /start চাপুন।"); return
        
    if status_info.get("role") != "admin" and not USER_DATA.get(user_id, {}).get("proxy"):
        await message.answer("❌ আপনি এখনও প্রক্সি সেট করেননি।\n"
                             "দয়া করে প্রথমে '⚙️ Set/Update Proxy' বাটন চেপে আপনার প্রক্সি সেট করুন।",
                             reply_markup=get_user_keyboard())
        return
            
    await message.answer("আপনি কোন সাইটের জন্য অ্যাকাউন্ট তৈরি করতে চান?",
                         reply_markup=get_site_selection_keyboard())

@dp.callback_query(F.data.startswith("select_site:"))
async def start_creation_process(query: types.CallbackQuery, state: FSMContext):
    user_id = query.from_user.id
    status_info = get_user_status(user_id)
    
    if status_info.get("status") != "active":
        await query.answer("❌ আপনার অ্যাক্সেসের মেয়াদ শেষ হয়ে গেছে। /start চাপুন।", show_alert=True); return
        
    site_key = query.data.split(":")[-1]
    if site_key not in SITE_CONFIGS:
        await query.answer("❌ অবৈধ সাইট।", show_alert=True); return
    
    if status_info.get("role") != "admin" and not USER_DATA.get(user_id, {}).get("proxy"):
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
                proxy_data = USER_DATA[message.from_user.id]["proxy"]
                proxy_host = proxy_data['host']; proxy_port = proxy_data['port']
                proxy_user = proxy_data['user']; proxy_pass = proxy_data['pass']
            except (KeyError, TypeError):
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


# --- অ্যাডমিন ম্যানেজমেন্ট হ্যান্ডলারগুলি ---
@dp.message(F.text == "🌐 Site Mgt")
async def handle_site_mgt(message: types.Message, state: FSMContext):
    if USER_DATA.get(message.from_user.id, {}).get("role") != "admin": return
    await state.clear()
    
    text = "🌐 <b>Site Management</b>\n\nবর্তমান সাইট:\n"
    if not SITE_CONFIGS:
        text += "(খালি)"
    else:
        for key, config in SITE_CONFIGS.items():
            text += f"- <b>{config['name']}</b> (key: <code>{key}</code>)\n"
            
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add New Site", callback_data="add_site")],
        [InlineKeyboardButton(text="➖ Remove Site", callback_data="remove_site")]
    ]))

@dp.callback_query(F.data == "add_site")
async def add_site_start(query: types.CallbackQuery, state: FSMContext):
    if USER_DATA.get(query.from_user.id, {}).get("role") != "admin": await query.answer("শুধুমাত্র অ্যাডমিন!", show_alert=True); return
    await query.message.answer("1/7: অনুগ্রহ করে সাইটের একটি ইউনিক <b>key</b> দিন (e.g., <code>newsite123</code>)",
                               reply_markup=get_fsm_cancel_keyboard())
    await state.set_state(UserData.adding_site_key)
    await query.answer()

@dp.message(UserData.adding_site_key)
async def add_site_key(message: types.Message, state: FSMContext):
    site_key = message.text.lower()
    if site_key in SITE_CONFIGS:
        await message.answer("❌ এই key টি ইতিমধ্যেই ব্যবহৃত হয়েছে। অন্য একটি key দিন।",
                             reply_markup=get_fsm_cancel_keyboard())
        return
    await state.update_data(site_key=site_key)
    await message.answer(f"2/7: সাইটের <b>Display Name</b> দিন (e.g., <code>NewSite123</code>)")
    await state.set_state(UserData.adding_site_name)

@dp.message(UserData.adding_site_name)
async def add_site_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer(f"3/7: সাইটের <b>API Endpoint</b> দিন\n(e.g., <code>https://newsite.com/api/user/signUp</code>)")
    await state.set_state(UserData.adding_site_endpoint)

@dp.message(UserData.adding_site_endpoint)
async def add_site_endpoint(message: types.Message, state: FSMContext):
    await state.update_data(api_endpoint=message.text)
    await message.answer(f"4/7: সাইটের <b>API Host</b> দিন (e.g., <code>newsite.com</code>)")
    await state.set_state(UserData.adding_site_host)

@dp.message(UserData.adding_site_host)
async def add_site_host(message: types.Message, state: FSMContext):
    await state.update_data(api_host=message.text)
    await message.answer(f"5/7: সাইটের <b>Origin</b> দিন (e.g., <code>https://newsite.com</code>)")
    await state.set_state(UserData.adding_site_origin)

@dp.message(UserData.adding_site_origin)
async def add_site_origin(message: types.Message, state: FSMContext):
    await state.update_data(origin=message.text)
    await message.answer(f"6/7: সাইটের <b>Referer</b> দিন (e.g., <code>https://newsite.com/</code>)")
    await state.set_state(UserData.adding_site_referer)

@dp.message(UserData.adding_site_referer)
async def add_site_referer(message: types.Message, state: FSMContext):
    await state.update_data(referer=message.text)
    await message.answer(f"7/7: সাইটের <b>Registration Host (reg_host)</b> দিন\n(e.g., <code>newsite.com</code>)")
    await state.set_state(UserData.adding_site_reghost)

@dp.message(UserData.adding_site_reghost)
async def add_site_reghost(message: types.Message, state: FSMContext):
    await state.update_data(reg_host=message.text)
    data = await state.get_data()
    
    site_config = {
        "site_key": data["site_key"],
        "name": data["name"],
        "api_endpoint": data["api_endpoint"],
        "api_host": data["api_host"],
        "origin": data["origin"],
        "referer": data["referer"],
        "reg_host": data["reg_host"]
    }
    
    await sites_collection.insert_one(site_config)
    SITE_CONFIGS[data["site_key"]] = site_config
    
    await message.answer(f"✅ সাইট <b>{data['name']}</b> সফলভাবে যোগ করা হয়েছে।", reply_markup=get_admin_keyboard())
    await state.clear()

@dp.callback_query(F.data == "remove_site")
async def remove_site_start(query: types.CallbackQuery, state: FSMContext):
    if USER_DATA.get(query.from_user.id, {}).get("role") != "admin": await query.answer("শুধুমাত্র অ্যাডমিন!", show_alert=True); return
    await query.message.answer("অনুগ্রহ করে যে সাইটটি ডিলিট করতে চান তার <b>key</b> টি টাইপ করুন:",
                               reply_markup=get_fsm_cancel_keyboard())
    await state.set_state(UserData.removing_site_key)
    await query.answer()

@dp.message(UserData.removing_site_key)
async def remove_site_finish(message: types.Message, state: FSMContext):
    site_key = message.text
    if site_key not in SITE_CONFIGS:
        await message.answer("❌ এই key-এর কোনো সাইট খুঁজে পাওয়া যায়নি।", reply_markup=get_admin_keyboard())
        await state.clear(); return
        
    await sites_collection.delete_one({"site_key": site_key})
    del SITE_CONFIGS[site_key]
    
    await message.answer(f"✅ সাইট (key: <code>{site_key}</code>) সফলভাবে ডিলিট করা হয়েছে।", reply_markup=get_admin_keyboard())
    await state.clear()

# --- সাব-অ্যাডমিন ম্যানেজমেন্ট (সরানো হয়েছে) ---
@dp.message(F.text == "🛡️ Sub-Admin Mgt")
async def handle_sub_admin_mgt(message: types.Message, state: FSMContext):
    if USER_DATA.get(message.from_user.id, {}).get("role") != "admin": return
    await message.answer("এই ফিচারটি বর্তমানে বন্ধ আছে।", reply_markup=get_admin_keyboard())

# --- ইউজার ব্যান ম্যানেজমেন্ট ---
@dp.message(F.text == "🚫 User Ban Mgt")
async def handle_user_ban_mgt(message: types.Message, state: FSMContext):
    if USER_DATA.get(message.from_user.id, {}).get("role") != "admin": return
    await state.clear()
    await message.answer("আপনি কী করতে চান?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Ban User", callback_data="ban_user")],
        [InlineKeyboardButton(text="✅ Unban User", callback_data="unban_user")]
    ]))

@dp.callback_query(F.data == "ban_user")
async def ban_user_start(query: types.CallbackQuery, state: FSMContext):
    if USER_DATA.get(query.from_user.id, {}).get("role") != "admin": await query.answer("শুধুমাত্র অ্যাডমিন!", show_alert=True); return
    await query.message.answer("অনুগ্রহ করে যে ইউজারকে ব্যান করতে চান তার <b>User ID</b> টাইপ করুন:",
                               reply_markup=get_fsm_cancel_keyboard())
    await state.set_state(UserData.banning_user_id)
    await query.answer()

@dp.message(UserData.banning_user_id)
async def ban_user_finish(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
    except ValueError:
        await message.answer("❌ User ID অবশ্যই একটি সংখ্যা হতে হবে।", reply_markup=get_fsm_cancel_keyboard()); return
    
    if user_id == ADMIN_ID:
        await message.answer("❌ আপনি অ্যাডমিনকে ব্যান করতে পারবেন না।", reply_markup=get_admin_keyboard()); return

    user_data = USER_DATA.get(user_id, {"user_id": user_id, "role": "user", "expires_at": 0})
    user_data["banned"] = True
    
    await users_collection.update_one({"user_id": user_id}, {"$set": {"banned": True}}, upsert=True)
    USER_DATA[user_id] = user_data
    
    await message.answer(f"🚫 ইউজার <code>{user_id}</code>-কে সফলভাবে ব্যান করা হয়েছে।", reply_markup=get_admin_keyboard())
    await state.clear()

@dp.callback_query(F.data == "unban_user")
async def unban_user_start(query: types.CallbackQuery, state: FSMContext):
    if USER_DATA.get(query.from_user.id, {}).get("role") != "admin": await query.answer("শুধুমাত্র অ্যাডমিন!", show_alert=True); return
    await query.message.answer("অনুগ্রহ করে যে ইউজারকে আনব্যান করতে চান তার <b>User ID</b> টাইপ করুন:",
                               reply_markup=get_fsm_cancel_keyboard())
    await state.set_state(UserData.unbanning_user_id)
    await query.answer()

@dp.message(UserData.unbanning_user_id)
async def unban_user_finish(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
    except ValueError:
        await message.answer("❌ User ID অবশ্যই একটি সংখ্যা হতে হবে।", reply_markup=get_fsm_cancel_keyboard()); return
    
    if not USER_DATA.get(user_id, {}).get("banned", False):
        await message.answer("✅ এই ইউজারটি ইতিমধ্যেই আনব্যানড আছে।", reply_markup=get_admin_keyboard())
        await state.clear(); return

    USER_DATA[user_id]["banned"] = False
    await users_collection.update_one({"user_id": user_id}, {"$set": {"banned": False}})
    
    await message.answer(f"✅ ইউজার <code>{user_id}</code>-কে সফলভাবে আনব্যান করা হয়েছে।", reply_markup=get_admin_keyboard())
    await state.clear()

# --- গ্রুপ ম্যানেজমেন্ট ---
@dp.message(F.text == "🔗 Group Mgt")
async def handle_group_mgt(message: types.Message, state: FSMContext):
    if USER_DATA.get(message.from_user.id, {}).get("role") != "admin": return
    await state.clear()
    
    group_id = BOT_CONFIG.get("group_id")
    group_link = BOT_CONFIG.get("group_link")
    
    text = f"🔗 <b>Group Join Management</b>\n\n"
    text += f"<b>Current Group ID:</b> <code>{group_id}</code>\n" if group_id else "<b>Current Group ID:</b> <code>Not Set</code>\n"
    text += f"<b>Current Group Link:</b> {group_link}\n" if group_link else "<b>Current Group Link:</b> <code>Not Set</code>\n"
            
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Set Group ID", callback_data="set_group_id")],
        [InlineKeyboardButton(text="✏️ Set Group Link", callback_data="set_group_link")]
    ]))

@dp.callback_query(F.data == "set_group_id")
async def set_group_id_start(query: types.CallbackQuery, state: FSMContext):
    if USER_DATA.get(query.from_user.id, {}).get("role") != "admin": await query.answer("শুধুমাত্র অ্যাডমিন!", show_alert=True); return
    await query.message.answer("অনুগ্রহ করে আপনার গ্রুপের <b>Chat ID</b> টাইপ করুন (এটি -100... দিয়ে শুরু হয়)।\n\n"
                               "<b>টিপ:</b> বটটিকে আপনার গ্রুপে অ্যাডমিন বানান, তারপর গ্রুপে <code>/get_id</code> টাইপ করুন।",
                               reply_markup=get_fsm_cancel_keyboard())
    await state.set_state(UserData.setting_group_id)
    await query.answer()
    
@dp.message(Command(commands=["get_id"]))
async def get_chat_id(message: types.Message):
    """গ্রুপের আইডি পাওয়ার জন্য একটি হেলপার কমান্ড"""
    await message.answer(f"এই চ্যাটের আইডি হলো: <code>{message.chat.id}</code>")

@dp.message(UserData.setting_group_id)
async def set_group_id_finish(message: types.Message, state: FSMContext):
    try:
        group_id = int(message.text)
    except ValueError:
        await message.answer("❌ Chat ID অবশ্যই একটি সংখ্যা হতে হবে।", reply_markup=get_fsm_cancel_keyboard()); return
    
    BOT_CONFIG["group_id"] = group_id
    await config_collection.update_one({"_id": "main_config"}, {"$set": {"group_id": group_id}}, upsert=True)
    await message.answer(f"✅ গ্রুপ আইডি <code>{group_id}</code>-তে সেট করা হয়েছে।", reply_markup=get_admin_keyboard())
    await state.clear()

@dp.callback_query(F.data == "set_group_link")
async def set_group_link_start(query: types.CallbackQuery, state: FSMContext):
    if USER_DATA.get(query.from_user.id, {}).get("role") != "admin": await query.answer("শুধুমাত্র অ্যাডমিন!", show_alert=True); return
    await query.message.answer("অনুগ্রহ করে আপনার গ্রুপের <b>Invite Link</b> টাইপ করুন (e.g., <code>https://t.me/mygroup</code>)",
                               reply_markup=get_fsm_cancel_keyboard())
    await state.set_state(UserData.setting_group_link)
    await query.answer()

@dp.message(UserData.setting_group_link)
async def set_group_link_finish(message: types.Message, state: FSMContext):
    group_link = message.text
    BOT_CONFIG["group_link"] = group_link
    await config_collection.update_one({"_id": "main_config"}, {"$set": {"group_link": group_link}}, upsert=True)
    await message.answer(f"✅ গ্রুপ লিঙ্ক {group_link}-এ সেট করা হয়েছে।", reply_markup=get_admin_keyboard())
    await state.clear()

# --- ধাপ ৯: বট চালু করা ---
async def main():
    """বট চালু করে"""
    await load_data_from_db() # <-- DB থেকে সব ডেটা লোড করা
    
    try:
        # --- *** এই লগ মেসেজটি ঠিক করা হয়েছে *** ---
        await bot.send_message(ADMIN_ID, f"✅ বট রিস্টার্ট/চালু হয়েছে! ({len(USER_DATA)} জন ইউজার লোডেড)")
    except Exception as e:
        logging.warning(f"অ্যাডমিনকে ({ADMIN_ID}) মেসেজ পাঠানো যায়নি: {e}")
    
    # --- Flask সার্ভারটি থ্রেডে চালু করা ---
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    
    logging.info("বট পোলিং শুরু করছে..."); 
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
