# -*- coding: utf-8 -*-
# یک‌بار روی سیستم خودت اجرا کن؛ خروجی رو بذار توی SESSION_STRING
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as c:
    print("\n=== SESSION_STRING (محرمانه، به کسی نده) ===\n")
    print(c.session.save())
