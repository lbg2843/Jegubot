import requests
import os
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()
else:
    env_path = Path(__file__).with_name(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

token = os.getenv("TELEGRAM_TRADING_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_TRADING_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

print(f"BOT_TOKEN : {token[:20]}..." if token else "BOT_TOKEN : 없음")
print(f"CHAT_ID   : {chat_id}")

now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
msg = f"🐢 Jegubot 초기 세팅 완료 - Telegram 연결 테스트 [{now}]"

url = f"https://api.telegram.org/bot{token}/sendMessage"
r = requests.post(url, json={"chat_id": chat_id, "text": msg}, timeout=10)
data = r.json()

if data.get("ok"):
    print(f"\n[OK] 전송 성공 (HTTP {r.status_code})")
    print(f"   message_id: {data['result']['message_id']}")
else:
    code = r.status_code
    desc = data.get("description", "")
    print(f"\n[FAIL] 전송 실패 (HTTP {code}): {desc}")
    if code == 401:
        print("   원인: BOT_TOKEN 잘못됨 -- BotFather에서 토큰 재확인")
    elif code == 400 and "chat not found" in desc:
        print("   원인: CHAT_ID 잘못됨 또는 봇에게 /start 미전송")
    else:
        print(f"   원인: {desc}")
