# -*- coding: utf-8 -*-
"""
InviteBot — یوزربات هوشمند + بات تأیید هویت + پنل ادمین
زیرساخت مثل app-5.py: pymongo (یا mongomock بدون MONGO_URI) + تنظیمات از Environment + AI سازگار با OpenAI

سه بخش در یک پروسه:
1) یوزربات (اکانت شخصی خودت، Telethon): به پیوی‌ها هوشمند جواب می‌ده. وقتی کسی «کلمه‌ی ثبت‌نام» بفرسته،
   تسک «دعوت N نفر» + لینک اختصاصی می‌گیره. با هر دعوتِ تأییدشده بهش خبر می‌ده و آخر تبریک می‌گه.
2) بات تأیید (Bot Token): لینک دعوت رو باز می‌کنه و فرد دعوت‌شده رو تأیید می‌کنه:
   کپچا ← شماره‌ی خودش (request_contact) ← بررسی دستگاه (مینی‌اپ) ← تحلیل الگوهای چندحسابی ← امتیازدهی. فقط بعد از تأیید، پاداش حساب می‌شه.
3) پنل ادمین: داخل همون بات، با /admin
"""
import csv, io, os, re, time, random, hashlib, secrets, asyncio, logging, threading
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, request, jsonify, Response
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from telethon import TelegramClient, events, Button, types
from telethon.sessions import StringSession
from telethon.errors import (FloodWaitError, SessionPasswordNeededError, PhoneCodeInvalidError,
                             PhoneCodeExpiredError, PhoneNumberInvalidError, PasswordHashInvalidError)

# ───────────────────────── تنظیمات (Environment) ─────────────────────────
API_ID         = int(os.environ["API_ID"])
API_HASH       = os.environ["API_HASH"]
SESSION_STRING = os.environ.get("SESSION_STRING", "")   # اختیاری؛ خالی = لاگین از پنل ادمین
BOT_TOKEN      = os.environ["BOT_TOKEN"]                 # بات تأیید + پنل ادمین
ADMIN_IDS      = {int(x) for x in re.split(r"[,\s]+", os.environ["ADMIN_IDS"].strip()) if x}
MONGO_URI      = os.environ.get("MONGO_URI", "").strip()  # خالی = حافظه‌ی موقت
SECRET_KEY     = os.environ.get("SECRET_KEY", BOT_TOKEN)  # برای هش شماره‌ها
AI_BASE_URL    = re.sub(r"/chat/completions/?$", "", os.environ.get("AI_BASE_URL", "").strip().rstrip("/"))
AI_API_KEY     = os.environ.get("AI_API_KEY", "")
AI_MODEL       = os.environ.get("AI_MODEL", "")
AI_ON          = bool(AI_BASE_URL and AI_API_KEY and AI_MODEL)
PUBLIC_URL     = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")  # آدرس https سرویس؛ برای بررسی دستگاه

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("invitebot")

if MONGO_URI:
    db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000).get_database("invitebot")
else:
    import mongomock
    db = mongomock.MongoClient().get_database("invitebot")
    log.warning("MONGO_URI تنظیم نشده؛ با هر ری‌استارت همه‌چی پاک می‌شه")
db.users.create_index("code", unique=True)
db.invites.create_index([("inviter", 1), ("status", 1)])
db.devices.create_index("did")
db.devices.create_index("fp")
db.devices.create_index("ip")

user = bot = None          # کلاینت‌ها داخل main ساخته می‌شن
BOT_USERNAME = ""

# ───────────────────────── تنظیمات قابل‌ویرایش از پنل ─────────────────────────
DEFAULTS = {
    "keywords": ["ثبت نام", "ثبتنام", "شروع", "register"],
    "rewards": [100, 100, 120],              # سنت؛ جمع = پاداش نهایی (۳.۲۰ دلار)، تعداد = تعداد دعوت لازم
    "min_score": 65,                          # حداقل امتیاز تأیید خودکار
    "review_score": 55,                       # بین این و بالایی → صف بررسی ادمین؛ کمتر → رد
    "allowed_prefixes": ["98"],               # پیش‌شماره‌های مجاز (خالی = همه)
    "blocked_prefixes": ["888"],              # شماره‌های ناشناس Fragment و ...
    "required_channel": os.environ.get("REQUIRED_CHANNEL", "").lstrip("@"),
    "velocity_max": 2,                        # اگه یه نفر در velocity_minutes دقیقه بیشتر از این تأیید بگیره → بررسی دستی
    "velocity_minutes": 15,
    "young_id": 7_500_000_000,                # آیدی بزرگ‌تر از این = حساب تازه‌ساز (تقریبی؛ از پنل تنظیم کن)
    "id_gap": 1_500_000,                      # دو دعوت‌شده‌ی یک دعوت‌کننده با آیدی نزدیک‌تر از این = ساخته‌شده‌ی هم‌زمان
    "max_flags": 3,                           # تعداد نشانه‌ی مشکوک برای رد خودکار
    "ai_enabled": True,
    "business_prompt": ("تو دستیار یه کسب‌وکار آنلاین هستی که در پیوی تلگرام جواب می‌ده. "
                        "[اینجا توضیح کسب‌وکارت، خدمات، لحن و قوانین رو بنویس — از پنل ادمین ویرایشش کن]"),
    "msg_task": ("🎉 ثبت‌نامت شروع شد!\n\n🎯 تسک مرحله ۱: {need} نفر رو با لینک اختصاصی خودت دعوت کن.\n\n"
                 "💰 پاداش هر دعوت تأییدشده:\n{rewards_list}\n\n🏁 پاداش نهایی مرحله ۱: {total}\n\n"
                 "🔗 لینک اختصاصی تو:\n{link}\n\n"
                 "⚠️ فقط افراد واقعی تأیید می‌شن (کپچا + تأیید شماره). حساب فیک یا تکراری حساب نمی‌شه.\n\n"
                 "📊 پیشرفت: {done}/{need}"),
    "msg_progress": ("✅ یک دعوت جدید تأیید شد!\n💰 پاداش این دعوت: {reward}\n"
                     "📊 پیشرفت: {done}/{need}\n💼 مجموع تا الان: {earned}\n\n🔗 {link}"),
    "msg_done": ("🎊🎉 تبریک! تسک مرحله ۱ رو کامل کردی!\n\n✅ {need} دعوت تأییدشده\n"
                 "💰 پاداش نهایی: {total}\n\nپاداشت برای پرداخت ثبت شد و تیم ما به‌زودی باهات هماهنگ می‌کنه. 🙏"),
    "msg_already": "🎉 تو قبلاً تسک مرحله ۱ رو کامل کردی و پاداشت ({total}) ثبت شده.",
}


def get_cfg():
    d = db.settings.find_one({"_id": "cfg"}) or {}
    return {**DEFAULTS, **{k: v for k, v in d.items() if k != "_id"}}


def set_cfg(k, v):
    db.settings.update_one({"_id": "cfg"}, {"$set": {k: v}}, upsert=True)


# ───────────────────────── ابزارها ─────────────────────────
def now():
    return datetime.now(timezone.utc)


def money(c):
    return f"${c / 100:.2f}"


def fmt(tpl, **kw):
    """جایگزینی امن {key}؛ آکولاد ناشناس خطا نمی‌ده"""
    return re.sub(r"\{(\w+)\}", lambda m: str(kw.get(m.group(1), m.group(0))), tpl)


def norm(s):
    s = (s or "").lower().replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_keyword(text, kws):
    t = norm(text)
    toks = t.split()
    for k in kws:
        k = norm(k)
        if k and (k == t or k in toks or (" " in k and k in t)):
            return True
    return False


def tg_http(method, **data):
    try:
        return requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=data, timeout=15).json()
    except Exception as e:
        log.warning("tg %s failed: %s", method, e)
        return {"ok": False}


async def bot_send(chat_id, text, **kw):
    """پیام از طریق Bot API (به access_hash نیاز نداره، بعد از ری‌استارت هم کار می‌کنه)"""
    return await asyncio.to_thread(tg_http, "sendMessage", chat_id=chat_id, text=text, **kw)


async def notify_admins(text):
    for a in ADMIN_IDS:
        await bot_send(a, text)


async def send_to_user(uid, text):
    """پیام از یوزربات به یه کاربر (با access_hash ذخیره‌شده)"""
    if user is None:
        return False
    u = db.users.find_one({"_id": uid}) or {}
    peer = types.InputPeerUser(uid, u["ah"]) if u.get("ah") is not None else uid
    for _ in range(2):
        try:
            await user.send_message(peer, text)
            return True
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
        except Exception:
            log.exception("send_to_user %s", uid)
            return False
    return False


def link_for(u):
    return f"https://t.me/{BOT_USERNAME}?start=r_{u['code']}"


def rewards_list(rewards):
    return "\n".join(f"• دعوت {i}: {money(c)}" for i, c in enumerate(rewards, 1))


# ───────────────────────── هوش مصنوعی (پاسخ یوزربات) ─────────────────────────
_rate = {}


def rate_ok(uid, n=20, per=3600):
    ts = [t for t in _rate.get(uid, []) if time.time() - t < per]
    ok = len(ts) < n
    if ok:
        ts.append(time.time())
    _rate[uid] = ts
    return ok


def system_prompt(cfg):
    rewards = cfg["rewards"]
    kw = cfg["keywords"][0] if cfg["keywords"] else "ثبت نام"
    return (cfg["business_prompt"] + "\n\n"
            "قوانین ثابت:\n"
            "- به زبان کاربر جواب بده (پیش‌فرض فارسی)، کوتاه و دوستانه، حداکثر ۴ جمله.\n"
            "- هیچ قیمت، قول، پاداش یا اطلاعاتی خارج از اطلاعات همین پیام نساز؛ اگه نمی‌دونی بگو «از تیم می‌پرسم».\n"
            "- اگه پرسید ربات یا هوش مصنوعی هستی، صادقانه بگو دستیار خودکاره.\n"
            f"- اگه کاربر می‌خواد ثبت‌نام کنه، بگو کلمه‌ی «{kw}» رو بفرسته.\n"
            f"- اطلاعات برنامه‌ی دعوت: برای ثبت‌نام باید {len(rewards)} نفر واقعی رو دعوت کنه؛ "
            f"پاداش هر دعوت: {', '.join(money(c) for c in rewards)}؛ پاداش نهایی مرحله ۱: {money(sum(rewards))}. "
            "پاداش فقط بعد از تأیید هویت افراد دعوت‌شده حساب می‌شه.")


def _ai_call(messages):
    r = requests.post(f"{AI_BASE_URL}/chat/completions",
                      headers={"Authorization": f"Bearer {AI_API_KEY}"},
                      json={"model": AI_MODEL, "max_tokens": 500, "temperature": 0.5, "messages": messages},
                      timeout=60)
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()


async def ai_answer(uid, text, cfg):
    hist = (db.chats.find_one({"_id": uid}) or {}).get("h", [])[-8:]
    msgs = ([{"role": "system", "content": system_prompt(cfg)}]
            + [{"role": x["r"], "content": x["c"]} for x in hist]
            + [{"role": "user", "content": text[:1000]}])
    try:
        out = (await asyncio.to_thread(_ai_call, msgs))[:1500]
    except Exception:
        log.exception("AI failed")
        return None
    if not out:
        return None
    db.chats.update_one({"_id": uid}, {"$push": {"h": {"$each": [
        {"r": "user", "c": text[:1000]}, {"r": "assistant", "c": out}], "$slice": -12}}}, upsert=True)
    return out


# ───────────────────────── یوزربات ─────────────────────────
def ensure_user(sender, ah):
    db.users.update_one({"_id": sender.id}, {
        "$set": {"name": (sender.first_name or "")[:40], "username": sender.username, "ah": ah, "last": now()},
        "$setOnInsert": {"code": secrets.token_urlsafe(6), "status": "new", "verified": 0, "earned": 0,
                         "created": now()}}, upsert=True)
    return db.users.find_one({"_id": sender.id})


def task_text(u, cfg):
    rewards = cfg["rewards"]
    if u["status"] == "completed":
        return fmt(cfg["msg_already"], total=money(u.get("earned", 0)), need=len(rewards))
    if u["status"] == "new":
        db.users.update_one({"_id": u["_id"]}, {"$set": {"status": "active", "reg_at": now()}})
    return fmt(cfg["msg_task"], need=len(rewards), done=u.get("verified", 0), total=money(sum(rewards)),
               rewards_list=rewards_list(rewards), link=link_for(u))


async def typing_pause(event, text_len=60):
    try:
        async with user.action(event.chat_id, "typing"):
            await asyncio.sleep(min(3.5, 0.8 + text_len / 80))
    except Exception:
        pass


async def on_dm(event):
    sender = await event.get_sender()
    if (not sender or getattr(sender, "bot", False) or sender.id == 777000
            or getattr(sender, "is_self", False)):
        return
    text = (event.raw_text or "").strip()
    if not text:
        return
    inp = await event.get_input_sender()
    u = ensure_user(sender, getattr(inp, "access_hash", None))
    if u.get("banned"):
        return
    cfg = get_cfg()
    try:
        await event.mark_read()
    except Exception:
        pass
    await asyncio.sleep(random.uniform(1.5, 5.0))
    if is_keyword(text, cfg["keywords"]):
        reply = task_text(u, cfg)
        await typing_pause(event, 30)
        await event.respond(reply)
        return
    if AI_ON and cfg["ai_enabled"] and rate_ok(sender.id):
        reply = await ai_answer(sender.id, text, cfg)
        if reply:
            await typing_pause(event, len(reply))
            await event.respond(reply)


# ───────────────────────── بات تأیید: کپچا ← شماره ← امتیاز ─────────────────────────
EMOJIS = ["🍎", "🚗", "🐱", "⭐", "🌹", "🎈", "🔔", "🍕"]
REASONS = {"flagged": "حساب توسط تلگرام مشکوک/اسکم علامت خورده", "prefix": "کشور شماره مجاز نیست",
           "dup_phone": "این شماره قبلاً برای حساب دیگه‌ای استفاده شده", "captcha": "کپچا ناموفق بود",
           "score": "امتیاز اعتماد حساب کافی نبود", "admin": "توسط تیم رد شد",
           "phone_fmt": "فرمت شماره معتبر نیست", "dup_device": "این دستگاه قبلاً برای حساب دیگه‌ای استفاده شده",
           "risk": "الگوی مشکوک (چند حساب از یک منبع)"}


async def send_captcha(event, uid):
    target = random.choice(EMOJIS)
    opts = random.sample(EMOJIS, 6)
    if target not in opts:
        opts[0] = target
        random.shuffle(opts)
    db.invites.update_one({"_id": uid}, {"$set": {"cap": target, "cap_at": time.time(), "step": "captcha"}})
    rows = [[Button.inline(e, data=f"cap:{e}".encode()) for e in opts[i:i + 3]] for i in (0, 3)]
    await event.respond(f"🤖 برای اثبات اینکه ربات نیستی، روی این شکل بزن:\n\n{target}", buttons=rows)


async def ask_phone(event):
    db.invites.update_one({"_id": event.sender_id}, {"$set": {"step": "phone"}})
    await event.respond("📱 حالا با دکمه‌ی پایین شماره‌ی خودت رو بفرست (فقط همین دکمه؛ شماره‌ی دیگران قبول نمی‌شه).",
                        buttons=[[Button.request_phone("📱 ارسال شماره‌ی من", resize=True, single_use=True)]])


async def resume(event, inv):
    if inv.get("step") == "captcha":
        await send_captcha(event, inv["_id"])
    elif inv.get("step") == "phone":
        await ask_phone(event)
    elif inv.get("step") == "device":
        await ask_device(inv["_id"])
    else:
        await finalize(event, inv["_id"])


async def on_start(event):
    uid = event.sender_id
    param = event.pattern_match.group(1)
    if uid in ADMIN_IDS and not param:
        t, b = render("home")
        return await event.respond(t, buttons=b)
    if not param or not param.startswith("r_"):
        return await event.respond("سلام! 👋 برای ثبت‌نام باید از لینک دعوت یکی از دوستانت وارد بشی.")
    inv = db.invites.find_one({"_id": uid})
    if inv:
        st = inv["status"]
        if st == "verified":
            return await event.respond("✅ تو قبلاً تأیید شدی. ممنون!")
        if st == "review":
            return await event.respond("⏳ تأییدت در حال بررسی توسط تیمه. نتیجه همین‌جا بهت اطلاع داده می‌شه.")
        if st == "rejected":
            return await event.respond("❌ متأسفانه تأیید هویتت موفق نبود.")
        return await resume(event, inv)
    inviter = db.users.find_one({"code": param[2:]})
    if not inviter:
        return await event.respond("این لینک معتبر نیست.")
    if inviter["_id"] == uid:
        return await event.respond("نمی‌تونی با لینک خودت ثبت‌نام کنی. 😉")
    if inviter["status"] != "active" or inviter.get("banned"):
        return await event.respond("این لینک دیگه فعال نیست.")
    sender = await event.get_sender()
    try:
        db.invites.insert_one({"_id": uid, "inviter": inviter["_id"], "status": "pending", "step": "captcha",
                               "attempts": 0, "created": now(), "name": (sender.first_name or "")[:40],
                               "username": sender.username})
    except DuplicateKeyError:
        return
    await event.respond("👋 خوش اومدی! برای تأیید هویت چند مرحله‌ی کوتاه داریم.")
    await send_captcha(event, uid)


async def on_captcha(event):
    uid = event.sender_id
    inv = db.invites.find_one({"_id": uid})
    if not inv or inv["status"] != "pending" or inv.get("step") != "captcha":
        return await event.answer("این مرحله منقضی شده.")
    pick = event.pattern_match.group(1).decode()
    elapsed = time.time() - inv.get("cap_at", 0)
    if pick == inv.get("cap") and 1.0 <= elapsed <= 180:
        await event.answer("✅")
        await event.delete()
        return await ask_phone(event)
    attempts = inv.get("attempts", 0) + 1
    db.invites.update_one({"_id": uid}, {"$set": {"attempts": attempts}})
    if attempts >= 3:
        await event.answer("ناموفق")
        return await reject_invite(uid, "captcha")
    await event.answer("❌ اشتباه بود، دوباره")
    await event.delete()
    await send_captcha(event, uid)


def phone_ok(phone):
    """ایران: فقط موبایل (989xxxxxxxxx)؛ بقیه‌ی کشورها: طول معقول"""
    if phone.startswith("98"):
        return bool(re.fullmatch(r"989\d{9}", phone))
    return 8 <= len(phone) <= 15


def _h(prefix, val):
    return hashlib.sha256((SECRET_KEY + prefix + val).encode()).hexdigest()


async def on_contact(event):
    uid = event.sender_id
    inv = db.invites.find_one({"_id": uid})
    if not inv or inv["status"] != "pending" or inv.get("step") != "phone":
        return
    c = event.message.contact
    if event.message.fwd_from or c.user_id != uid:
        return await event.respond("❌ شماره باید مال خودت باشه و با همون دکمه ارسال بشه.")
    phone = re.sub(r"\D", "", c.phone_number or "")
    cfg = get_cfg()
    if cfg["allowed_prefixes"] and not any(phone.startswith(p) for p in cfg["allowed_prefixes"]):
        return await reject_invite(uid, "prefix")
    if any(phone.startswith(p) for p in cfg["blocked_prefixes"]):
        return await reject_invite(uid, "prefix")
    if not phone_ok(phone):
        return await reject_invite(uid, "phone_fmt")
    ph = hashlib.sha256((SECRET_KEY + phone).encode()).hexdigest()
    try:
        db.phones.insert_one({"_id": ph, "uid": uid, "t": now()})
    except DuplicateKeyError:
        if (db.phones.find_one({"_id": ph}) or {}).get("uid") != uid:
            return await reject_invite(uid, "dup_phone")
    nxt = "device" if PUBLIC_URL else "channel"
    # phone_block: همه‌ی ارقام به‌جز ۳ رقم آخر (سیم‌کارت‌های پشت‌سرهم یک بسته)
    db.invites.update_one({"_id": uid}, {"$set": {"step": nxt, "phone_hash": ph[:12],
                                                  "phone_block": _h("pb", phone[:-3])[:12]}})
    await event.respond("✅ شماره تأیید شد.", buttons=Button.clear())
    if nxt == "device":
        return await ask_device(uid)
    await finalize(event, uid)


async def ask_device(uid):
    tok = secrets.token_urlsafe(16)
    db.invites.update_one({"_id": uid}, {"$set": {"step": "device", "dtoken": tok, "dtoken_at": time.time()}})
    await bot_send(uid, "🔐 آخرین مرحله: بررسی امنیتی دستگاه. دکمه‌ی زیر رو بزن و صبر کن تا «انجام شد» ببینی.",
                   reply_markup={"inline_keyboard": [[{"text": "🔐 شروع بررسی", "web_app": {"url": f"{PUBLIC_URL}/verify?t={tok}"}}]]})


async def on_dev(event):
    uid = event.sender_id
    inv = db.invites.find_one({"_id": uid})
    if not inv or inv["status"] != "pending":
        return await event.answer("این مرحله منقضی شده.")
    if inv.get("step") != "channel":
        return await event.answer("اول بررسی دستگاه رو کامل کن.", alert=True)
    await event.answer()
    await finalize(event, uid)


_ch_warned = False


async def is_member(ch, uid):
    try:
        p = await bot.get_permissions(ch, uid)
        return p is not None and not getattr(p, "has_left", False) and not getattr(p, "is_banned", False)
    except Exception as e:
        if "participant" in type(e).__name__.lower() or "participant" in str(e).lower():
            return False
        log.warning("membership check failed (بات باید ادمین کانال باشه): %s", e)
        global _ch_warned
        if not _ch_warned:
            _ch_warned = True
            await notify_admins("⚠️ چک عضویت کانال خطا داد؛ مطمئن شو بات ادمین کانال اجباریه. تا اون موقع تأییدها متوقف می‌مونن.")
        return False


def id_points(uid):
    """تخمین تقریبی سن حساب از روی آیدی عددی (هرچه کوچک‌تر، قدیمی‌تر). آستانه‌ها تقریبی‌ان."""
    if uid < 2_000_000_000: return 20
    if uid < 6_000_000_000: return 12
    if uid < 7_000_000_000: return 5
    return 0


def score_user(u):
    if getattr(u, "scam", False) or getattr(u, "fake", False):
        return 0, ["telegram:scam/fake"], "flagged"
    if getattr(u, "deleted", False) or getattr(u, "bot", False):
        return 0, ["deleted/bot"], "flagged"
    s, why = 55, ["captcha+phone:55"]
    p = id_points(u.id)
    s += p; why.append(f"age:{p}")
    ph = getattr(u, "photo", None)
    if ph and not isinstance(ph, types.UserProfilePhotoEmpty):
        s += 10; why.append("photo:10")
    if getattr(u, "username", None):
        s += 5; why.append("username:5")
    if getattr(u, "premium", False):
        s += 10; why.append("premium:10")
    return s, why, None


LIVE = ["verified", "review", "pending"]


def risk_flags(uid, inv, cfg):
    """تشخیص الگوی چندحسابی. خروجی: (لیست نشانه‌ها, دلیل رد قطعی یا None)"""
    flags, inviter = [], inv["inviter"]
    sib = {"inviter": inviter, "_id": {"$ne": uid}, "status": {"$in": LIVE}}
    sibs = list(db.invites.find(sib))
    sib_ids = [x["_id"] for x in sibs]
    if uid >= cfg["young_id"]:
        flags.append("young-account")
    recent = db.invites.count_documents({"inviter": inviter, "status": "verified",
                                         "verified_at": {"$gte": now() - timedelta(minutes=cfg["velocity_minutes"])}})
    if recent >= cfg["velocity_max"]:
        flags.append("velocity")
    if db.invites.find_one({"_id": inviter, "inviter": uid}):
        flags.append("mutual-invite")
    # شماره‌های هم‌بسته (سیم‌کارت‌های خریداری‌شده‌ی پشت‌سرهم)
    pb = inv.get("phone_block")
    if pb:
        if any(x.get("phone_block") == pb for x in sibs):
            flags.append("phone-block")
        inviter_inv = db.invites.find_one({"_id": inviter}) or {}
        if inviter_inv.get("phone_block") == pb:
            flags.append("phone-block-inviter")
    # حساب‌هایی که هم‌زمان ساخته شدن (آیدی عددی نزدیک)
    gap = cfg["id_gap"]
    if gap > 0 and uid >= 6_000_000_000:
        if any(i >= 6_000_000_000 and abs(i - uid) <= gap for i in sib_ids):
            flags.append("id-cluster")
        if inviter >= 6_000_000_000 and abs(inviter - uid) <= gap:
            flags.append("id-near-inviter")
    # دستگاه / IP
    dev = db.devices.find_one({"_id": uid})
    if dev:
        others = [d["_id"] for d in db.devices.find({"did": dev["did"], "_id": {"$ne": uid}})]
        if others and db.invites.count_documents({"_id": {"$in": others}, "status": {"$in": ["verified", "review"]}}):
            return flags, "dup_device"
        if db.devices.find_one({"_id": inviter, "did": dev["did"]}):
            return flags, "dup_device"
        if sib_ids and db.devices.find_one({"_id": {"$in": sib_ids}, "fp": dev["fp"]}):
            flags.append("same-fingerprint")
        if sib_ids and db.devices.find_one({"_id": {"$in": sib_ids}, "ip": dev["ip"]}):
            flags.append("same-ip")
        if db.devices.find_one({"_id": inviter, "$or": [{"fp": dev["fp"]}, {"ip": dev["ip"]}]}):
            flags.append("same-source-as-inviter")
    return flags, None


def risk_of(inviter_id):
    bad = tot = 0
    for i in db.invites.find({"inviter": inviter_id, "status": {"$in": ["verified", "review"]}}):
        tot += 1
        if [f for f in i.get("flags", []) if f != "young-account"]:
            bad += 1
    return bad, tot


async def finalize(event, uid):
    inv = db.invites.find_one({"_id": uid})
    if not inv or inv["status"] != "pending" or inv.get("step") != "channel":
        return
    cfg = get_cfg()
    ch = cfg["required_channel"]
    if ch and not await is_member(ch, uid):
        return await event.respond("📢 برای ادامه اول عضو کانال شو، بعد دکمه‌ی «عضو شدم» رو بزن.", buttons=[
            [Button.url("📢 عضویت در کانال", f"https://t.me/{ch}")], [Button.inline("✅ عضو شدم", data=b"chk")]])
    sender = await event.get_sender()
    score, why, hard = score_user(sender)
    if hard:
        return await reject_invite(uid, hard, score, why)
    flags, hard = risk_flags(uid, inv, cfg)
    why = why + flags
    db.invites.update_one({"_id": uid}, {"$set": {"flags": flags}})
    if hard:
        return await reject_invite(uid, hard, score, why)
    blocking = [f for f in flags if f != "young-account"]
    if len(flags) >= cfg["max_flags"]:
        await reject_invite(uid, "risk", score, why)
    elif score >= cfg["min_score"] and not blocking:
        await verify_invite(uid, score, why)
    elif score >= cfg["review_score"]:
        db.invites.update_one({"_id": uid}, {"$set": {"status": "review", "score": score, "reasons": why}})
        await event.respond("⏳ اطلاعاتت ثبت شد و برای بررسی نهایی به تیم رفت. نتیجه همین‌جا اعلام می‌شه.")
        await notify_admins("🕵️ یک دعوت برای بررسی دستی در صفه. /admin")
    else:
        await reject_invite(uid, "score", score, why)


async def on_chk(event):
    await event.answer()
    await finalize(event, event.sender_id)


async def reject_invite(uid, reason, score=None, why=None):
    upd = {"status": "rejected", "reason": reason, "at": now()}
    if score is not None:
        upd.update(score=score, reasons=why or [])
    r = db.invites.update_one({"_id": uid, "status": {"$in": ["pending", "review"]}}, {"$set": upd})
    if r.modified_count:
        await bot_send(uid, f"❌ تأیید هویت موفق نبود: {REASONS.get(reason, reason)}.\n"
                            "اگه فکر می‌کنی اشتباهه به پشتیبانی پیام بده.", reply_markup={"remove_keyboard": True})


def credit_invite(inv):
    """پاداش رو اتمیک به دعوت‌کننده می‌ده. خروجی: (done, reward, finished, earned) یا None"""
    rewards = get_cfg()["rewards"]
    need = len(rewards)
    for _ in range(5):
        u = db.users.find_one({"_id": inv["inviter"]})
        if not u or u["status"] != "active" or u.get("banned"):
            return None
        idx = u.get("verified", 0)
        if idx >= need:
            return None
        r = rewards[idx]
        upd = {"$inc": {"verified": 1, "earned": r}}
        if idx + 1 == need:
            upd["$set"] = {"status": "completed", "completed_at": now()}
        if db.users.update_one({"_id": u["_id"], "verified": idx, "status": "active"}, upd).modified_count:
            return idx + 1, r, idx + 1 == need, u.get("earned", 0) + r
    return None


async def verify_invite(uid, score=None, why=None, by="auto"):
    upd = {"status": "verified", "verified_at": now(), "by": by}
    if score is not None:
        upd.update(score=score, reasons=why or [])
    ok_states = ["pending", "review"] + (["rejected"] if by.startswith("admin") else [])
    if not db.invites.update_one({"_id": uid, "status": {"$in": ok_states}}, {"$set": upd}).modified_count:
        return
    inv = db.invites.find_one({"_id": uid})
    await bot_send(uid, "✅ هویتت تأیید شد. ممنون! 🙏", reply_markup={"remove_keyboard": True})
    res = credit_invite(inv)
    if not res:
        db.invites.update_one({"_id": uid}, {"$set": {"reward": 0}})
        return
    done, reward, finished, earned = res
    db.invites.update_one({"_id": uid}, {"$set": {"reward": reward}})
    cfg = get_cfg()
    u = db.users.find_one({"_id": inv["inviter"]})
    text = fmt(cfg["msg_done"] if finished else cfg["msg_progress"], reward=money(reward), done=done,
               need=len(cfg["rewards"]), earned=money(earned), total=money(earned), link=link_for(u))
    await send_to_user(inv["inviter"], text)
    if finished:
        await notify_admins(f"🏁 {u.get('name', '')} (@{u.get('username') or '-'}) — {u['_id']} تسک مرحله ۱ رو کامل کرد: "
                            f"{money(earned)}\n/admin ← پرداخت‌ها")


async def on_other(event):
    await event.respond("برای ثبت‌نام از لینک دعوت دوستت استفاده کن. 🙂")


# ───────────────────────── پنل ادمین ─────────────────────────
admin_state = {}          # admin_id -> ("edit", key) | ("find",) | ("bc",)
bc_pending = {}
PAGE = 8

LABELS = {
    "keywords": "🔑 کلمات ثبت‌نام", "rewards": "💰 پاداش دعوت‌ها", "min_score": "✅ حداقل امتیاز تأیید",
    "review_score": "🕵️ حداقل امتیاز بررسی", "allowed_prefixes": "🌍 پیش‌شماره‌ی مجاز",
    "blocked_prefixes": "⛔ پیش‌شماره‌ی ممنوع", "required_channel": "📢 کانال اجباری",
    "young_id": "🆕 آیدی حساب تازه", "id_gap": "🆔 فاصله‌ی آیدی مشکوک", "max_flags": "🚩 سقف نشانه‌ی مشکوک",
    "velocity_max": "⚡ سقف دعوت سریع", "velocity_minutes": "⏱ بازه‌ی دعوت سریع (دقیقه)",
    "business_prompt": "🧠 توضیح کسب‌وکار (AI)", "msg_task": "✉️ پیام تسک", "msg_progress": "✉️ پیام پیشرفت",
    "msg_done": "🎊 پیام تبریک", "msg_already": "✉️ پیام تکمیل‌شده",
}
HELP = {
    "keywords": "کلمات رو با ویرگول جدا کن. مثال: ثبت نام, شروع",
    "rewards": "پاداش هر دعوت به دلار با ویرگول. مثال: 1, 1, 1.2\n(تعداد = تعداد دعوت لازم، جمع = پاداش نهایی)",
    "min_score": "عدد ۰ تا ۱۰۰", "review_score": "عدد ۰ تا ۱۰۰ (کمتر از حداقل تأیید)",
    "allowed_prefixes": "مثال: 98 یا چندتا با ویرگول. برای «همه‌ی کشورها» بفرست -",
    "blocked_prefixes": "مثال: 888, 1 . برای خالی کردن بفرست -",
    "required_channel": "یوزرنیم کانال (بات باید ادمینش باشه). برای حذف بفرست -",
    "velocity_max": "عدد", "velocity_minutes": "عدد",
    "young_id": "آیدی عددی؛ حساب‌های با آیدی بزرگ‌تر «تازه» حساب می‌شن (تقریبی). مثال: 7500000000",
    "id_gap": "اگه آیدی دو دعوت‌شده‌ی یک نفر کمتر از این فاصله داشته باشه یعنی هم‌زمان ساخته شدن. 0 = خاموش",
    "max_flags": "اگه تعداد نشانه‌های مشکوک یه دعوت به این عدد برسه خودکار رد می‌شه (پیشنهاد: 3)",
    "business_prompt": "توضیح کسب‌وکار و لحن پاسخ هوش مصنوعی",
}
PLACEHOLDERS = "\nجاگذاری‌ها: {link} {need} {done} {reward} {earned} {total} {rewards_list}"


def B(label, data):
    return Button.inline(label, data=("adm:" + data).encode())


def disp(cfg, k):
    v = cfg[k]
    if k == "rewards":
        return " + ".join(money(c) for c in v) + f" = {money(sum(v))}"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "—"
    return (str(v) or "—")[:60].replace("\n", " ")


def render(page, *a):
    cfg = get_cfg()
    need = len(cfg["rewards"])
    if page == "home":
        c = lambda col, q: db[col].count_documents(q)
        unpaid = list(db.users.find({"status": "completed", "paid": {"$ne": True}}))
        text = ("🛠 پنل ادمین\n\n"
                f"👥 کاربران: {c('users', {})} (فعال {c('users', {'status': 'active'})} | کامل {c('users', {'status': 'completed'})} | بن {c('users', {'banned': True})})\n"
                f"📨 دعوت‌ها: تأییدشده {c('invites', {'status': 'verified'})} | در انتظار {c('invites', {'status': 'pending'})} | "
                f"بررسی {c('invites', {'status': 'review'})} | ردشده {c('invites', {'status': 'rejected'})}\n"
                f"💸 پرداخت‌نشده: {len(unpaid)} نفر — {money(sum(u.get('earned', 0) for u in unpaid))}\n"
                f"🧠 هوش مصنوعی: {'روشن' if cfg['ai_enabled'] and AI_ON else 'خاموش'}\n"
                f"🤖 یوزربات: {'متصل ✅' if user else 'قطع ❌ (از دکمه‌ی اتصال وارد شو)'}")
        rc = c("invites", {"status": "review"})
        return text, ([[B("👥 کاربران", "users:0"), B("🔎 جستجو", "find")],
                      [B(f"🕵️ بررسی ({rc})", "rev"), B(f"💸 پرداخت‌ها ({len(unpaid)})", "pay")],
                      [B("❌ ردشده‌ها", "rej"), B("📤 خروجی CSV", "csv")],
                      [B("⚙️ تنظیمات", "set"), B("📣 ارسال همگانی", "bc")],
                      [B("🔑 اتصال با کد", "ulogin"), B("📷 اتصال با QR", "uqr")]]
                      + ([[B("🚪 قطع یوزربات", "ulogout")]] if user else []))
    if page == "users":
        p = int(a[0])
        total = db.users.count_documents({})
        rows = []
        for u in db.users.find({}).sort("created", -1).skip(p * PAGE).limit(PAGE):
            ic = "⛔" if u.get("banned") else {"new": "⚪", "active": "🟡", "completed": "🟢"}.get(u["status"], "⚪")
            rows.append([B(f"{ic} {u.get('name', '')[:14]} {u.get('verified', 0)}/{need}", f"u:{u['_id']}")])
        nav = []
        if p > 0: nav.append(B("◀️", f"users:{p - 1}"))
        if (p + 1) * PAGE < total: nav.append(B("▶️", f"users:{p + 1}"))
        return f"👥 کاربران ({total}) — صفحه {p + 1}", rows + ([nav] if nav else []) + [[B("🔙", "home")]]
    if page == "u":
        u = db.users.find_one({"_id": int(a[0])})
        if not u:
            return "کاربر پیدا نشد.", [[B("🔙", "home")]]
        invs = list(db.invites.find({"inviter": u["_id"]}).limit(10))
        il = "\n".join(f"  • {i['_id']} {i.get('name', '')} — {i['status']} ({i.get('score', '-')})"
                       f"{' 🚩' if [f for f in i.get('flags', []) if f != 'young-account'] else ''}" for i in invs) or "  —"
        bad, tot = risk_of(u["_id"])
        text = (f"👤 {u.get('name', '')} @{u.get('username') or '-'}\n🆔 {u['_id']}\n"
                f"وضعیت: {u['status']}{' ⛔بن' if u.get('banned') else ''}\n"
                f"پیشرفت: {u.get('verified', 0)}/{need} | درآمد: {money(u.get('earned', 0))}"
                f"{' | پرداخت‌شده ✅' if u.get('paid') else ''}\n🚩 دعوت‌های مشکوک: {bad}/{tot}\n\nدعوت‌ها:\n{il}")
        ban = B("✅ آنبن", f"unban:{u['_id']}") if u.get("banned") else B("⛔ بن", f"ban:{u['_id']}")
        return text, [[ban, B("♻️ ریست پیشرفت", f"reset:{u['_id']}")], [B("🔙", "users:0")]]
    if page == "rev":
        inv = db.invites.find_one({"status": "review"}, sort=[("created", 1)])
        left = db.invites.count_documents({"status": "review"})
        if not inv:
            return "🕵️ صف بررسی خالیه. ✅", [[B("🔙", "home")]]
        ir = db.users.find_one({"_id": inv["inviter"]}) or {}
        bad, tot = risk_of(inv["inviter"])
        text = (f"🕵️ بررسی ({left} مورد)\n\n👤 دعوت‌شده: {inv.get('name', '')} @{inv.get('username') or '-'} — {inv['_id']}\n"
                f"📊 امتیاز: {inv.get('score')}\n🧾 {', '.join(inv.get('reasons', []))}\n"
                f"👥 دعوت‌کننده: {ir.get('name', '')} @{ir.get('username') or '-'} — {inv['inviter']}\n"
                f"🚩 سابقه‌ی مشکوک دعوت‌کننده: {bad}/{tot}")
        return text, [[B("✅ تأیید", f"ra:{inv['_id']}"), B("❌ رد", f"rr:{inv['_id']}")], [B("🔙", "home")]]
    if page == "rej":
        invs = list(db.invites.find({"status": "rejected"}).sort("at", -1).limit(8))
        if not invs:
            return "❌ ردشده‌ای نیست.", [[B("🔙", "home")]]
        text = "❌ آخرین ردشده‌ها:\n\n" + "\n".join(
            f"• {i.get('name', '')} @{i.get('username') or '-'} — {i['_id']} — {REASONS.get(i.get('reason'), i.get('reason'))} ({i.get('score', '-')})"
            for i in invs)
        rows = [[B(f"✅ تأیید دستی: {i.get('name', '')[:12]}", f"ra:{i['_id']}")] for i in invs]
        return text, rows + [[B("🔙", "home")]]
    if page == "pay":
        us = list(db.users.find({"status": "completed", "paid": {"$ne": True}}).limit(10))
        if not us:
            return "💸 پرداخت‌نشده‌ای نیست. ✅", [[B("🔙", "home")]]
        text = "💸 تکمیل‌شده‌های پرداخت‌نشده:\n\n" + "\n".join(
            f"• {u.get('name', '')} @{u.get('username') or '-'} — {u['_id']} — {money(u.get('earned', 0))}"
            + (lambda r: f" ⚠️ مشکوک {r[0]}/{r[1]}" if r[0] else "")(risk_of(u["_id"])) for u in us)
        rows = [[B(f"✅ پرداخت شد: {u.get('name', '')[:12]} {money(u.get('earned', 0))}", f"pd:{u['_id']}")] for u in us]
        return text, rows + [[B("🔙", "home")]]
    if page == "set":
        text = "⚙️ تنظیمات\n\n" + "\n".join(f"{LABELS[k]}: {disp(cfg, k)}" for k in LABELS)
        keys = list(LABELS)
        rows = [[B(LABELS[k], f"edit:{k}") for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
        rows.append([B(f"🧠 هوش مصنوعی: {'روشن' if cfg['ai_enabled'] else 'خاموش'} (تغییر)", "tog")])
        return text, rows + [[B("🔙", "home")]]
    return "؟", [[B("🔙", "home")]]


def parse_setting(key, raw):
    raw = raw.strip()
    if key == "keywords":
        v = [norm(x) for x in re.split(r"[,\n،]+", raw) if norm(x)]
        if not v: raise ValueError("حداقل یک کلمه")
        return v
    if key == "rewards":
        try:
            v = [int(Decimal(p) * 100) for p in re.split(r"[,\s،]+", raw) if p]
        except InvalidOperation:
            raise ValueError("فقط عدد (مثلاً 1, 1, 1.2)")
        if not (1 <= len(v) <= 10) or any(c <= 0 for c in v): raise ValueError("بین ۱ تا ۱۰ عدد مثبت")
        return v
    if key in ("min_score", "review_score", "velocity_max", "velocity_minutes", "young_id", "id_gap", "max_flags"):
        if not raw.isdigit(): raise ValueError("باید عدد باشه")
        return int(raw)
    if key in ("allowed_prefixes", "blocked_prefixes"):
        if raw == "-": return []
        v = [re.sub(r"\D", "", x) for x in re.split(r"[,\s،]+", raw) if re.sub(r"\D", "", x)]
        if not v: raise ValueError("پیش‌شماره‌ی معتبر بفرست")
        return v
    if key == "required_channel":
        if raw == "-": return ""
        raw = raw.lstrip("@").replace("https://t.me/", "")
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]{4,31}$", raw): raise ValueError("یوزرنیم کانال معتبر نیست")
        return raw
    if not raw: raise ValueError("متن خالیه")
    return raw[:3500]


async def show(event, page, *a):
    t, b = render(page, *a)
    try:
        await event.edit(t, buttons=b)
    except Exception:
        pass


async def admin_cmd(event):
    t, b = render("home")
    await event.respond(t, buttons=b)


async def admin_cb(event):
    a = event.pattern_match.group(1).decode().split(":")
    act, uid = a[0], event.sender_id
    cfg = get_cfg()
    await event.answer()
    if act == "ra":
        await verify_invite(int(a[1]), by=f"admin:{uid}"); return await show(event, "rev")
    if act == "rr":
        await reject_invite(int(a[1]), "admin"); return await show(event, "rev")
    if act == "pd":
        db.users.update_one({"_id": int(a[1])}, {"$set": {"paid": True, "paid_at": now()}}); return await show(event, "pay")
    if act in ("ban", "unban"):
        db.users.update_one({"_id": int(a[1])}, {"$set": {"banned": act == "ban"}}); return await show(event, "u", a[1])
    if act == "reset":
        db.users.update_one({"_id": int(a[1])}, {"$set": {"verified": 0, "earned": 0, "status": "active", "paid": False}})
        return await show(event, "u", a[1])
    if act == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "name", "username", "status", "verified", "earned_usd", "paid"])
        for u in db.users.find({}):
            w.writerow([u["_id"], u.get("name", ""), u.get("username") or "", u["status"], u.get("verified", 0),
                        f"{u.get('earned', 0) / 100:.2f}", bool(u.get("paid"))])
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        data.name = "users.csv"
        await bot.send_file(uid, data, caption="📤 خروجی کاربران")
        return
    if act == "ulogin":
        admin_state[uid] = ("login", "phone")
        return await event.edit("📱 شماره‌ی اکانت یوزربات رو با کد کشور بفرست.\nمثال: +989123456789",
                                buttons=[[B("🔙 لغو", "home")]])
    if act == "uqr":
        await event.edit("📷 در حال ساخت QR ...", buttons=[[B("🔙 لغو", "home")]])
        asyncio.create_task(qr_login_flow(event, uid))
        return
    if act == "ulogout":
        await detach_userbot()
        db.settings.delete_one({"_id": "session"})
        return await show(event, "home")
    if act == "tog":
        set_cfg("ai_enabled", not cfg["ai_enabled"]); return await show(event, "set")
    if act == "edit" and a[1] in LABELS:
        admin_state[uid] = ("edit", a[1])
        extra = HELP.get(a[1], "متن جدید رو بفرست." + PLACEHOLDERS)
        return await event.edit(f"✏️ {LABELS[a[1]]}\n\nمقدار فعلی:\n{cfg[a[1]] if not isinstance(cfg[a[1]], list) else disp(cfg, a[1])}\n\n{extra}\n\nمقدار جدید رو بفرست:",
                                buttons=[[B("🔙 لغو", "set")]])
    if act == "find":
        admin_state[uid] = ("find",)
        return await event.edit("🔎 آیدی عددی یا یوزرنیم کاربر رو بفرست:", buttons=[[B("🔙 لغو", "home")]])
    if act == "bc":
        admin_state[uid] = ("bc",)
        return await event.edit("📣 متن پیام همگانی رو بفرست.\n⚠️ فقط به کسانی می‌ره که خودشون به یوزربات پیام دادن؛ "
                                "ارسال با فاصله انجام می‌شه تا اکانتت محدود نشه.", buttons=[[B("🔙 لغو", "home")]])
    if act == "bcgo":
        text = bc_pending.pop(uid, None)
        if text:
            asyncio.create_task(run_broadcast(uid, text))
            return await event.edit("🚀 ارسال شروع شد؛ پایانش گزارش می‌دم.", buttons=[[B("🔙", "home")]])
        return await show(event, "home")
    admin_state.pop(uid, None)
    await show(event, act if act in ("home", "users", "u", "rev", "pay", "set", "rej") else "home", *a[1:])


async def admin_text(event):
    uid = event.sender_id
    st = admin_state.pop(uid, None)
    if not st:
        return
    raw = event.raw_text
    if st[0] == "login":
        return await login_step(event, st, raw)
    if st[0] == "edit":
        try:
            set_cfg(st[1], parse_setting(st[1], raw))
        except ValueError as e:
            admin_state[uid] = st
            return await event.respond(f"❌ {e}\nدوباره بفرست:")
        t, b = render("set")
        return await event.respond("✅ ذخیره شد.\n\n" + t, buttons=b)
    if st[0] == "find":
        q = raw.strip().lstrip("@")
        u = db.users.find_one({"_id": int(q)}) if q.isdigit() else db.users.find_one({"username": q})
        if not u:
            return await event.respond("کاربر پیدا نشد.", buttons=[[B("🔙", "home")]])
        t, b = render("u", u["_id"])
        return await event.respond(t, buttons=b)
    if st[0] == "bc":
        bc_pending[uid] = raw
        return await event.respond(f"پیش‌نمایش:\n\n{raw}\n\nارسال بشه؟",
                                   buttons=[[B("✅ ارسال", "bcgo"), B("❌ لغو", "home")]])


async def run_broadcast(admin, text):
    ok = fail = 0
    for u in list(db.users.find({"banned": {"$ne": True}})):
        if await send_to_user(u["_id"], text): ok += 1
        else: fail += 1
        await asyncio.sleep(random.uniform(1.5, 3.0))
    await bot_send(admin, f"📣 ارسال تموم شد. موفق: {ok} | ناموفق: {fail}")


# ───────────────────────── لاگین یوزربات از داخل پنل (بدون SESSION_STRING) ─────────────────────────
login_client = None
login_data = {}


async def _userbot_runner(client):
    try:
        await client.run_until_disconnected()
    except Exception:
        log.exception("userbot stopped")


async def attach_userbot(client):
    global user
    user = client
    user.add_event_handler(on_dm, events.NewMessage(incoming=True, func=lambda e: e.is_private))
    asyncio.create_task(_userbot_runner(client))


async def detach_userbot():
    global user
    old, user = user, None
    if old:
        try:
            await old.disconnect()
        except Exception:
            pass


async def start_saved_userbot():
    sess = SESSION_STRING or (db.settings.find_one({"_id": "session"}) or {}).get("s", "")
    if not sess:
        return False
    c = TelegramClient(StringSession(sess), API_ID, API_HASH)
    try:
        await c.connect()
        if not await c.is_user_authorized():
            await c.disconnect()
            return False
        await c.get_me()
    except Exception:
        log.exception("userbot connect failed")
        return False
    await attach_userbot(c)
    return True


async def login_done(event):
    global login_client
    c, login_client = login_client, None
    db.settings.update_one({"_id": "session"}, {"$set": {"s": c.session.save(), "at": now()}}, upsert=True)
    await detach_userbot()
    await attach_userbot(c)
    me = await c.get_me()
    await event.respond(f"✅ یوزربات وصل شد: {me.first_name or ''} @{me.username or '-'}\n"
                        "از الان به پیوی‌ها جواب می‌ده. (سشن توی دیتابیس ذخیره شد)", buttons=[[B("🔙 پنل", "home")]])


async def send_qr(uid, url):
    cap = ("📷 با اکانت یوزربات در یه دستگاه دیگه: تلگرام ← Settings ← Devices ← Link Desktop Device ← اسکن این QR.\n"
           "⏱ هر ~۳۰ ثانیه QR جدید می‌فرستم؛ آخری رو اسکن کن.")
    try:
        import qrcode
        buf = io.BytesIO()
        qrcode.make(url).save(buf, format="PNG")
        buf.seek(0)
        buf.name = "qr.png"
        try:
            await bot.send_file(uid, buf, caption=cap, buttons=[[Button.url("🔗 باز کردن لینک لاگین", url)]])
        except Exception:
            buf.seek(0)
            await bot.send_file(uid, buf, caption=cap + "\n\n" + url)
    except Exception:
        await bot_send(uid, cap + "\n\n" + url)


async def qr_login_flow(event, uid):
    global login_client
    try:
        if login_client:
            await login_client.disconnect()
        login_client = TelegramClient(StringSession(), API_ID, API_HASH)
        await login_client.connect()
        qr = await login_client.qr_login()
        for _ in range(6):
            await send_qr(uid, qr.url)
            try:
                await qr.wait(timeout=28)
                return await login_done(event)
            except asyncio.TimeoutError:
                await qr.recreate()
            except SessionPasswordNeededError:
                admin_state[uid] = ("login", "pw")
                return await bot_send(uid, "🔐 رمز تأیید دو مرحله‌ای اکانت رو بفرست:")
        await bot_send(uid, "⌛ QR منقضی شد. دوباره از پنل «اتصال با QR» رو بزن.")
    except Exception as e:
        log.exception("qr login failed")
        await bot_send(uid, f"❌ خطا در QR: {type(e).__name__}")


async def login_step(event, st, raw):
    global login_client
    uid, step = event.sender_id, st[1]
    cancel = [[B("🔙 لغو", "home")]]
    try:
        if step == "phone":
            phone = "+" + re.sub(r"\D", "", raw)
            if login_client:
                await login_client.disconnect()
            login_client = TelegramClient(StringSession(), API_ID, API_HASH)
            await login_client.connect()
            sent = await login_client.send_code_request(phone)
            login_data[uid] = {"phone": phone, "hash": sent.phone_code_hash}
            admin_state[uid] = ("login", "code")
            return await event.respond(
                "📨 کد به تلگرام اکانت یوزربات اومد.\n⚠️ ارقام رو با خط تیره یا فاصله بفرست (مثلاً 1-2-3-4-5)؛ "
                "اگه کد رو سرراست بفرستی تلگرام باطلش می‌کنه.", buttons=cancel)
        if step == "code":
            d = login_data.get(uid) or {}
            try:
                await login_client.sign_in(d["phone"], re.sub(r"\D", "", raw), phone_code_hash=d["hash"])
            except SessionPasswordNeededError:
                admin_state[uid] = ("login", "pw")
                return await event.respond("🔐 رمز تأیید دو مرحله‌ای اکانت رو بفرست:", buttons=cancel)
            return await login_done(event)
        if step == "pw":
            await login_client.sign_in(password=raw)
            try:
                await event.delete()
            except Exception:
                pass
            return await login_done(event)
    except PhoneCodeInvalidError:
        admin_state[uid] = ("login", "code")
        return await event.respond("❌ کد اشتباهه. دوباره بفرست (با خط تیره):", buttons=cancel)
    except PhoneCodeExpiredError:
        admin_state[uid] = ("login", "phone")
        return await event.respond("⌛ کد منقضی شد. دوباره شماره رو بفرست تا کد جدید بیاد:", buttons=cancel)
    except PhoneNumberInvalidError:
        admin_state[uid] = ("login", "phone")
        return await event.respond("❌ شماره نامعتبره. مثل +989123456789 بفرست:", buttons=cancel)
    except PasswordHashInvalidError:
        admin_state[uid] = ("login", "pw")
        return await event.respond("❌ رمز اشتباهه. دوباره بفرست:", buttons=cancel)
    except FloodWaitError as e:
        return await event.respond(f"⏳ تلگرام محدود کرده؛ {e.seconds} ثانیه بعد دوباره از پنل شروع کن.", buttons=cancel)
    except Exception as e:
        log.exception("login failed")
        return await event.respond(f"❌ خطا: {type(e).__name__}\nاز پنل دوباره شروع کن.", buttons=cancel)


# ───────────────────────── اجرا ─────────────────────────
web = Flask(__name__)


@web.get("/")
def health():
    return "ok"


VERIFY_HTML = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>body{font-family:system-ui,Tahoma;background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#111);
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
#m{font-size:18px;padding:24px;line-height:2}</style></head><body><div id="m">⏳ در حال بررسی امنیتی دستگاه ...</div>
<script>
(async()=>{
const tg=window.Telegram&&Telegram.WebApp;if(tg){tg.ready();tg.expand();}
const out=t=>document.getElementById('m').textContent=t;
try{
const q=new URLSearchParams(location.search).get('t')||'';
const rnd=()=>Array.from(crypto.getRandomValues(new Uint8Array(16))).map(b=>b.toString(16).padStart(2,'0')).join('');
let did=null;try{did=localStorage.getItem('did')}catch(e){}
if(!did){const m=document.cookie.match(/(?:^|; )did=([a-f0-9]{32})/);if(m)did=m[1];}
if(!did)did=rnd();
try{localStorage.setItem('did',did)}catch(e){}
document.cookie='did='+did+'; max-age=315360000; path=/; SameSite=None; Secure';
let c='';try{const cv=document.createElement('canvas');cv.width=240;cv.height=60;const x=cv.getContext('2d');
x.textBaseline='top';x.font='16px Arial';x.fillStyle='#f60';x.fillRect(10,5,100,30);x.fillStyle='#069';
x.fillText('Fp check 1.2',12,8);c=cv.toDataURL();}catch(e){}
let g='';try{const gl=document.createElement('canvas').getContext('webgl');const e=gl.getExtension('WEBGL_debug_renderer_info');
g=gl.getParameter(e.UNMASKED_RENDERER_WEBGL)+'|'+gl.getParameter(e.UNMASKED_VENDOR_WEBGL);}catch(e){}
const raw=[c,g,screen.width,screen.height,devicePixelRatio,Intl.DateTimeFormat().resolvedOptions().timeZone,
(navigator.languages||[]).join(','),navigator.hardwareConcurrency,navigator.deviceMemory,navigator.platform].join('||');
const buf=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(raw));
const fp=Array.from(new Uint8Array(buf)).map(b=>b.toString(16).padStart(2,'0')).join('');
const r=await fetch('/verify/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({t:q,did:did,fp:fp})});
const j=await r.json();
if(j.ok){out('✅ انجام شد! به بات برگرد و دکمه‌ی «ادامه» رو بزن.');setTimeout(()=>{if(tg)tg.close()},1200);}
else out('❌ بررسی ناموفق بود یا لینک منقضی شده. به بات برگرد و دوباره شروع کن.');
}catch(e){out('❌ خطا در بررسی. دوباره تلاش کن.');}
})();
</script></body></html>"""


@web.get("/verify")
def verify_page():
    return Response(VERIFY_HTML, mimetype="text/html")


@web.post("/verify/api")
def verify_api():
    d = request.get_json(silent=True) or {}
    t, did, fp = str(d.get("t", ""))[:64], str(d.get("did", "")), str(d.get("fp", ""))
    if not (re.fullmatch(r"[A-Za-z0-9_-]{16,64}", t) and re.fullmatch(r"[a-f0-9]{32}", did)
            and re.fullmatch(r"[a-f0-9]{64}", fp)):
        return jsonify(ok=False), 400
    inv = db.invites.find_one({"dtoken": t, "step": "device", "status": "pending"})
    if not inv or time.time() - inv.get("dtoken_at", 0) > 900:
        return jsonify(ok=False), 400
    ip = (request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    claimed = db.invites.find_one_and_update({"_id": inv["_id"], "dtoken": t, "step": "device"},
                                             {"$set": {"step": "channel"}, "$unset": {"dtoken": ""}})
    if not claimed:
        return jsonify(ok=False), 400
    db.devices.update_one({"_id": inv["_id"]}, {"$set": {"did": did, "fp": fp, "ip": _h("ip", ip)[:16],
                                                        "inviter": inv["inviter"], "t": now()}}, upsert=True)
    tg_http("sendMessage", chat_id=inv["_id"], text="✅ بررسی دستگاه انجام شد. برای ادامه دکمه‌ی زیر رو بزن.",
            reply_markup={"inline_keyboard": [[{"text": "✅ ادامه", "callback_data": "dev"}]]})
    return jsonify(ok=True)


async def main():
    global user, bot, BOT_USERNAME
    threading.Thread(target=lambda: web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))),
                     daemon=True).start()
    bot = TelegramClient(StringSession(), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    BOT_USERNAME = (await bot.get_me()).username

    priv = lambda e: e.is_private
    adm = lambda e: e.is_private and e.sender_id in ADMIN_IDS
    bot.add_event_handler(on_start, events.NewMessage(pattern=r"^/start(?:\s+(\S+))?$", func=priv))
    bot.add_event_handler(admin_cmd, events.NewMessage(pattern=r"^/admin$", func=adm))
    bot.add_event_handler(on_captcha, events.CallbackQuery(pattern=rb"cap:(.+)"))
    bot.add_event_handler(on_chk, events.CallbackQuery(pattern=rb"chk"))
    bot.add_event_handler(on_dev, events.CallbackQuery(pattern=rb"dev"))
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL تنظیم نشده؛ مرحله‌ی بررسی دستگاه (ضدچندحسابی) غیرفعاله")
    bot.add_event_handler(admin_cb, events.CallbackQuery(pattern=rb"adm:(.*)", func=lambda e: e.sender_id in ADMIN_IDS))
    bot.add_event_handler(on_contact, events.NewMessage(func=lambda e: e.is_private and e.message.contact is not None))
    bot.add_event_handler(admin_text, events.NewMessage(
        func=lambda e: adm(e) and bool(e.raw_text) and not e.raw_text.startswith("/")))
    bot.add_event_handler(on_other, events.NewMessage(
        func=lambda e: e.is_private and e.sender_id not in ADMIN_IDS and e.message.contact is None
        and not (e.raw_text or "").startswith("/")))
    log.info("آماده — بات تأیید: @%s | ادمین‌ها: %s", BOT_USERNAME, ADMIN_IDS)
    if await start_saved_userbot():
        log.info("یوزربات وصل شد")
    else:
        log.warning("یوزربات وصل نیست؛ از /admin ← اتصال یوزربات وارد شو")
        await notify_admins("🔑 یوزربات هنوز وصل نیست. بات تأیید رو باز کن: /admin ← 🔑 اتصال یوزربات")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
