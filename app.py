
from flask import Flask, render_template, request, redirect, jsonify
import threading
import time
import json
import os
import re
import requests
from datetime import datetime, timedelta
from collections import defaultdict

app = Flask(__name__)

# ===== 配置 =====
COOKIE_FILE = "cookie.txt"
try:
    with open(COOKIE_FILE, "r", encoding="utf-8") as f:
        COOKIE = f.read().strip()
except FileNotFoundError:
    COOKIE = ""
    print("⚠️ 请创建 cookie.txt 文件并填入 Cookie")

TELEGRAM_BOT_TOKEN = "7561649005:AAFKMgsQRxBB1yYQ-9gzgoIdoKCyXrtTuCo"
TELEGRAM_CHAT_ID = "1829072365"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Cookie": COOKIE,
    "Referer": "https://www.pandalive.co.kr",
    "Accept": "*/*",
}

API_TEMPLATE = "https://api.pandalive.co.kr/v1/live/play?action=watch&userId={user_id}"
SAVE_DIR = "监测日志"
os.makedirs(SAVE_DIR, exist_ok=True)

monitor_ids_file = "monitor_ids.json"
ff_cache_file = "ff_cache.json"
log_lines = []
monitor_ids = []
monitor_interval = 120
monitor_enabled = True
recent_ff_links = defaultdict(list)
stop_event = threading.Event()

# ===== 工具函数 =====
def log(msg):
    print(msg)
    log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    if len(log_lines) > 200:
        log_lines.pop(0)

def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        requests.post(url, data=payload, timeout=10)
    except:
        log("❌ Telegram 发送失败")

def extract_ff(text):
    text = text.replace('\/', '/')
    match = re.search(r'https://ff[^"\s]+\.m3u8\?token=[^"\s]+', text)
    return match.group(0) if match else None

def extract_usw(m3u8_text):
    pattern = re.compile(r'#EXT-X-STREAM-INF:.*RESOLUTION=(\d+x\d+).*?\n(https://usw[^\s]+)')
    streams = re.findall(pattern, m3u8_text)
    if not streams:
        return None
    for res, url in streams:
        if res == "1920x1080":
            return url
    return sorted(streams, key=lambda x: int(x[0].split("x")[1]), reverse=True)[0][1]

def load_ids():
    global monitor_ids
    if os.path.exists(monitor_ids_file):
        with open(monitor_ids_file, "r", encoding="utf-8") as f:
            monitor_ids = json.load(f)

def save_ids():
    with open(monitor_ids_file, "w", encoding="utf-8") as f:
        json.dump(monitor_ids, f)

def load_ff_cache():
    if os.path.exists(ff_cache_file):
        with open(ff_cache_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
            for uid, items in raw.items():
                for item in items:
                    ts = datetime.fromisoformat(item["timestamp"])
                    if datetime.now() - ts <= timedelta(minutes=10):
                        recent_ff_links[uid].append({"url": item["url"], "timestamp": ts})

def save_ff_cache():
    serializable = {
        uid: [{"url": item["url"], "timestamp": item["timestamp"].isoformat()} 
              for item in items if datetime.now() - item["timestamp"] <= timedelta(minutes=10)]
        for uid, items in recent_ff_links.items()
    }
    with open(ff_cache_file, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False)

# ===== 主监控线程 =====
def monitor():
    while not stop_event.is_set():
        if not monitor_enabled:
            time.sleep(5)
            continue

        for uid in monitor_ids:
            try:
                url = API_TEMPLATE.format(user_id=uid)
                resp = requests.get(url, headers=HEADERS, timeout=10)
                text = resp.text

                if '"isLive":true' in text:
                    ff_url = extract_ff(text)
                    if ff_url:
                        if not any(item["url"] == ff_url for item in recent_ff_links[uid]):
                            recent_ff_links[uid].append({"url": ff_url, "timestamp": datetime.now()})
                            log(f"{uid}: ✅ 主播在线，已缓存链接")

                elif '"code":"needCoinPurchase"' in text or '"code":"needPw"' in text:
                    room_type = "粉丝房" if "needCoinPurchase" in text else "密码房"
                    usable = [item for item in recent_ff_links[uid]
                              if datetime.now() - item["timestamp"] <= timedelta(minutes=10)][-3:]
                    log(f"{uid}: 🔐 进入{room_type}")
                    for item in usable:
                        try:
                            r = requests.get(item["url"], headers=HEADERS, timeout=10)
                            if r.status_code == 200:
                                usw = extract_usw(r.text)
                                if usw:
                                    send_telegram(f"<b>{uid}</b> 进入{room_type}\n直播源：\n{usw}")
                                    log(f"{uid}: 📤 已发送直播源")
                        except Exception as e:
                            log(f"{uid}: 访问 ff 异常 {e}")
                    recent_ff_links[uid].clear()

                elif '"code":"castEnd"' in text:
                    log(f"{uid}: ⛔️ 主播下播")

                elif '"code":"needUnlimitItem"' in text:
                    log(f"{uid}: 🚫 房间满，跳过")

                else:
                    try：
                        json_data = json.loads(text)
                        message = json_data.get("message", "")
                        log(f"{uid}: ⚠️ 接口返回错误：{message}")
                    except:
                        log(f"{uid}: ⚠️ 未知返回 {text[:80]}")

            except Exception as e:
                log(f"{uid}: ❌ 请求异常：{e}")

        now = datetime.now()
        for uid in list(recent_ff_links.keys()):
            recent_ff_links[uid] = [item for item in recent_ff_links[uid] if now - item["timestamp"] <= timedelta(minutes=10)]

        save_ff_cache()
        time.sleep(monitor_interval)

# ===== Flask 接口 =====
@app.route("/")
def index():
    return render_template("index.html", ids=monitor_ids, interval=monitor_interval, running=monitor_enabled)

@app.route("/add", methods=["POST"])
def add_id():
    uid = request.form.get("uid")
    if uid and uid not in monitor_ids:
        monitor_ids.append(uid)
        save_ids()
    return redirect("/")

@app.route("/delete", methods=["POST"])
def delete_ids():
    ids = request.form.getlist("uids")
    for uid in ids:
        if uid in monitor_ids:
            monitor_ids.remove(uid)
    save_ids()
    return redirect("/")

@app.route("/toggle", methods=["POST"])
def toggle_monitor():
    global monitor_enabled
    monitor_enabled = not monitor_enabled
    return redirect("/")

@app.route("/set_interval", methods=["POST"])
def set_interval():
    global monitor_interval
    try:
        monitor_interval = int(request.form.get("interval"))
    except:
        pass
    return redirect("/")

@app.route("/logs")
def get_logs():
    return "<br>".join(log_lines[-100:])

# ===== 启动 =====
if __name__ == "__main__":
    load_ids()
    load_ff_cache()
    threading.Thread(target=monitor, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
