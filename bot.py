import os
import re
import io
import glob
import json
import time
import uuid
import shutil
import secrets
import string
import asyncio
import unicodedata
import subprocess
import socks
from telethon import TelegramClient, events
from aiohttp import web
import aiohttp
import qrcode

# ==================== НАСТРОЙКИ ====================
API_ID = 38989287                          # Ваш API ID
API_HASH = '04f55718c613f3cada36fd1690c1d16a'            # Ваш API Hash
BOT_TOKEN = '8905900627:AAG9tJ22lvVL-RcTfDquicnbvFpVzh0dDi0'       # Токен бота
DOMAIN = 'devopslymar.ru'                  # Ваш домен
QUOTA_SIZE = '1G'                          # Размер персонального диска
# ===================================================

BASE_DIR = '/opt/tg-cloud'
FILES_DIR = os.path.join(BASE_DIR, 'files')
DISKS_DIR = os.path.join(BASE_DIR, 'disks')
DB_PATH = os.path.join(BASE_DIR, 'filebrowser.db')
USERS_DB = os.path.join(BASE_DIR, 'users.json')
SESSION_PATH = os.path.join(BASE_DIR, 'bot_session')

RESERVED_LOGINS = {
    'admin', 'administrator', 'root', 'system', 
    'filebrowser', 'files', 'sessions', 'to_telegram', 'from_telegram', 'disks'
}

bot = TelegramClient(
    SESSION_PATH, 
    API_ID, 
    API_HASH, 
    use_ipv6=False,
    proxy=(socks.SOCKS5, '127.0.0.1', 40000)
)

qr_sessions = {}
bot_username = "devops_lymar_cloud_bot"  # Укажите ваш юзернейм бота без @ на всякий случай

def load_users():
    if os.path.exists(USERS_DB):
        try:
            with open(USERS_DB, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {"by_id": {}, "by_name": {}, "passwords": {}}

def save_user(user_id, username, password=None):
    data = load_users()
    if "passwords" not in data:
        data["passwords"] = {}
    data["by_id"][str(user_id)] = username
    data["by_name"][username.lower()] = user_id
    if password:
        data["passwords"][username.lower()] = password
    with open(USERS_DB, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def generate_password(length=12):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def sanitize_and_validate_username(raw_text):
    if not raw_text:
        return None, "Логин не может быть пустым."
    cleaned = unicodedata.normalize('NFC', raw_text.strip())
    if len(cleaned) < 2 or len(cleaned) > 20:
        return None, "Длина логина должна быть от 2 до 20 символов."
    pattern = r'^[a-zA-Zа-яА-ЯёЁ0-9_-]+$'
    if not re.match(pattern, cleaned):
        return None, "Логин может содержать только русские или английские буквы и цифры без пробелов."
    if cleaned.lower() in RESERVED_LOGINS:
        return None, "Этот логин зарезервирован системой."
    if os.path.basename(cleaned) != cleaned or '/' in cleaned or '\\' in cleaned:
        return None, "Недопустимый формат логина."
    return cleaned, None

def create_user_virtual_disk(username, quota=QUOTA_SIZE):
    os.makedirs(DISKS_DIR, exist_ok=True)
    img_path = os.path.join(DISKS_DIR, f"{username}.img")
    mount_point = os.path.join(FILES_DIR, username)
    subprocess.run(["truncate", "-s", quota, img_path], check=True)
    subprocess.run(["mkfs.ext4", "-F", "-q", "-m", "0", img_path], check=True)
    os.makedirs(mount_point, exist_ok=True)
    subprocess.run(["mount", "-o", "loop", img_path, mount_point], check=True)
    os.makedirs(os.path.join(mount_point, 'from_telegram'), exist_ok=True)
    os.makedirs(os.path.join(mount_point, 'to_telegram'), exist_ok=True)

def create_filebrowser_user(username, password):
    cmd = [
        "/usr/local/bin/filebrowser", "users", "add", 
        username, password, 
        "--scope", f"/{username}", 
        "--perm.admin=false", 
        "-d", DB_PATH
    ]
    try:
        subprocess.run(["systemctl", "stop", "filebrowser"], timeout=5)
        time.sleep(1)
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        subprocess.run(["systemctl", "start", "filebrowser"], timeout=5)
        if res.returncode != 0:
            return False, res.stderr
        return True, None
    except Exception as e:
        subprocess.run(["systemctl", "start", "filebrowser"], timeout=5)
        return False, str(e)

def update_filebrowser_password(username, new_password):
    cmd = [
        "/usr/local/bin/filebrowser", "users", "update", 
        username, "--password", new_password,
        "-d", DB_PATH
    ]
    try:
        subprocess.run(["systemctl", "stop", "filebrowser"], timeout=5)
        time.sleep(1)
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        subprocess.run(["systemctl", "start", "filebrowser"], timeout=5)
        return res.returncode == 0
    except Exception:
        subprocess.run(["systemctl", "start", "filebrowser"], timeout=5)
        return False

async def get_filebrowser_jwt(username, password):
    url = "http://127.0.0.1:8080/api/login"
    payload = {"username": username, "password": password}
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    token = await resp.text()
                    return token.strip('"')
    except Exception as e:
        print(f"[JWT Error] {e}")
    return None

# ==================== HTML СТРАНИЦА АВТОРИЗАЦИИ ====================
LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Облачное хранилище — Вход</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body { background: #0f172a; display: flex; align-items: center; justify-content: center; min-height: 100vh; color: #f8fafc; }
        .card { background: #1e293b; padding: 32px; border-radius: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); width: 100%; max-width: 380px; text-align: center; }
        h1 { font-size: 22px; margin-bottom: 20px; font-weight: 600; }
        .tabs { display: flex; background: #0f172a; padding: 4px; border-radius: 12px; margin-bottom: 24px; }
        .tab { flex: 1; padding: 10px; font-size: 14px; font-weight: 500; cursor: pointer; border-radius: 8px; border: none; background: transparent; color: #94a3b8; transition: 0.2s; }
        .tab.active { background: #2563eb; color: #fff; }
        .content { display: none; }
        .content.active { display: block; }
        .qr-box { background: #fff; padding: 12px; border-radius: 16px; display: inline-block; margin-bottom: 16px; position: relative; }
        .qr-box img { display: block; width: 200px; height: 200px; }
        .subtext { font-size: 13px; color: #94a3b8; line-height: 1.5; }
        .input-group { margin-bottom: 16px; text-align: left; }
        label { font-size: 13px; color: #94a3b8; margin-bottom: 6px; display: block; }
        input { width: 100%; padding: 12px; border-radius: 10px; border: 1px solid #334155; background: #0f172a; color: #fff; font-size: 14px; outline: none; }
        input:focus { border-color: #2563eb; }
        .btn { width: 100%; padding: 12px; border-radius: 10px; border: none; background: #2563eb; color: #fff; font-size: 15px; font-weight: 600; cursor: pointer; margin-top: 8px; }
        .btn:hover { background: #1d4ed8; }
        .error { color: #ef4444; font-size: 13px; margin-top: 12px; display: none; }
        .status { margin-top: 12px; font-size: 13px; color: #38bdf8; display: flex; align-items: center; justify-content: center; gap: 8px; }
    </style>
</head>
<body>
    <div class="card">
        <h1>Облачный диск</h1>
        <div class="tabs">
            <button class="tab active" onclick="switchTab('qr')">По QR-коду</button>
            <button class="tab" onclick="switchTab('password')">По паролю</button>
        </div>

        <div id="tab-qr" class="content active">
            <div class="qr-box">
                <img id="qr-img" src="" alt="QR Code">
            </div>
            <p class="subtext">Наведите камеру смартфона для быстрого входа через Telegram</p>
            <div class="status" id="qr-status">Ожидание сканирования...</div>
        </div>

        <div id="tab-password" class="content">
            <form onsubmit="handlePasswordLogin(event)">
                <div class="input-group">
                    <label>Логин</label>
                    <input type="text" id="username" required autocomplete="username">
                </div>
                <div class="input-group">
                    <label>Пароль</label>
                    <input type="password" id="password" required autocomplete="current-password">
                </div>
                <button type="submit" class="btn">Войти</button>
                <div class="error" id="login-error">Неверный логин или пароль</div>
            </form>
        </div>
    </div>

    <script>
        let currentSessionId = '';
        let pollInterval = null;

        function switchTab(type) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.content').forEach(c => c.classList.remove('active'));
            if(type === 'qr') {
                document.querySelectorAll('.tab')[0].classList.add('active');
                document.getElementById('tab-qr').classList.add('active');
                startQRSession();
            } else {
                document.querySelectorAll('.tab')[1].classList.add('active');
                document.getElementById('tab-password').classList.add('active');
                if(pollInterval) clearInterval(pollInterval);
            }
        }

        async function startQRSession() {
            if(pollInterval) clearInterval(pollInterval);
            try {
                const res = await fetch('/auth/new');
                const data = await res.json();
                currentSessionId = data.session_id;
                document.getElementById('qr-img').src = '/auth/qr/' + currentSessionId + '.png';
                document.getElementById('qr-status').innerText = 'Ожидание сканирования...';
                pollInterval = setInterval(checkQRStatus, 1500);
            } catch(e) {
                document.getElementById('qr-status').innerText = 'Ошибка загрузки QR-кода';
            }
        }

        async function checkQRStatus() {
            if(!currentSessionId) return;
            try {
                const res = await fetch('/auth/status/' + currentSessionId);
                const data = await res.json();
                if(data.status === 'approved') {
                    clearInterval(pollInterval);
                    document.getElementById('qr-status').innerText = 'Успешно! Входим...';
                    localStorage.setItem('jwt', data.token);
                    setTimeout(() => window.location.href = '/', 500);
                } else if(data.status === 'expired') {
                    clearInterval(pollInterval);
                    document.getElementById('qr-status').innerText = 'QR-код устарел. Обновляем...';
                    setTimeout(startQRSession, 1000);
                }
            } catch(e) {}
        }

        async function handlePasswordLogin(e) {
            e.preventDefault();
            const u = document.getElementById('username').value;
            const p = document.getElementById('password').value;
            const err = document.getElementById('login-error');
            err.style.display = 'none';

            try {
                const res = await fetch('/api/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({username: u, password: p})
                });
                if(res.ok) {
                    const token = await res.text();
                    localStorage.setItem('jwt', token.replace(/"/g, ''));
                    window.location.href = '/';
                } else {
                    err.style.display = 'block';
                }
            } catch(ex) {
                err.innerText = 'Ошибка соединения';
                err.style.display = 'block';
            }
        }

        startQRSession();
    </script>
</body>
</html>
"""

# ==================== ВЕБ-ОБРАБОТЧИКИ (AIOHTTP) ====================
async def web_login_page(request):
    return web.Response(text=LOGIN_HTML, content_type='text/html')

async def web_new_session(request):
    session_id = uuid.uuid4().hex[:12]
    qr_sessions[session_id] = {
        "status": "pending",
        "token": None,
        "created_at": time.time()
    }
    return web.json_response({"session_id": session_id})

async def web_qr_image(request):
    session_id = request.match_info.get('session_id', '')
    if session_id not in qr_sessions:
        return web.Response(status=404)

    tg_url = f"https://t.me/{bot_username}?start=qr_{session_id}"
    qr = qrcode.QRCode(box_size=8, border=1)
    qr.add_data(tg_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return web.Response(body=buf.getvalue(), content_type='image/png')

async def web_check_status(request):
    session_id = request.match_info.get('session_id', '')
    session = qr_sessions.get(session_id)

    if not session:
        return web.json_response({"status": "expired"})

    if time.time() - session["created_at"] > 120:
        del qr_sessions[session_id]
        return web.json_response({"status": "expired"})

    return web.json_response({
        "status": session["status"],
        "token": session.get("token")
    })

# ==================== ОБРАБОТЧИКИ TELEGRAM ====================
@bot.on(events.NewMessage(pattern='/start'))
async def handle_start(event):
    if not event.is_private:
        return

    user_id = event.sender_id
    users_data = load_users()
    registered_username = users_data["by_id"].get(str(user_id))

    text = event.raw_text.strip()
    if text.startswith('/start qr_'):
        session_id = text.replace('/start qr_', '').strip()
        
        if session_id not in qr_sessions or qr_sessions[session_id]["status"] != "pending":
            await event.respond("⚠️ Этот QR-код устарел или уже использован. Обновите страницу на ПК.")
            return

        if not registered_username:
            await event.respond("❌ Сначала отправьте мне желаемый логин для регистрации.")
            return

        password = users_data.get("passwords", {}).get(registered_username.lower())
        if not password:
            await event.respond("⚠️ Требуется обновить пароль. Нажмите /reset, затем отсканируйте код снова.")
            return

        token = await get_filebrowser_jwt(registered_username, password)
        if token:
            qr_sessions[session_id]["status"] = "approved"
            qr_sessions[session_id]["token"] = token
            await event.respond(f"✅ **Вход на ПК разрешен!**\nВы успешно вошли как `{registered_username}`.")
        else:
            await event.respond("❌ Не удалось войти. Проверьте пароль через команду /reset.")
        return

    if registered_username:
        user_dir = os.path.join(FILES_DIR, registered_username)
        if not os.path.exists(user_dir):
            del users_data["by_id"][str(user_id)]
            users_data["by_name"] = {k: v for k, v in users_data["by_name"].items() if str(v) != str(user_id)}
            with open(USERS_DB, 'w', encoding='utf-8') as f:
                json.dump(users_data, f, indent=2, ensure_ascii=False)
            registered_username = None

    if registered_username:
        await event.respond(
            "👋 **С возвращением!**\n\n"
            f"🌐 **Адрес диска:** https://{DOMAIN}\n"
            f"👤 **Твой логин:** `{registered_username}`\n"
            f"💾 **Квота диска:** {QUOTA_SIZE}\n\n"
            "📁 *Отправь мне любой файл, и он появится на сайте.*\n"
            "🔑 Если забыл пароль, нажми: /reset"
        )
        return

    await event.respond(
        "👋 **Добро пожаловать в персональное облачное хранилище!**\n\n"
        f"Тебе будет выделен личный диск на **{QUOTA_SIZE}**.\n\n"
        "Отправь мне желаемый **логин** для входа на сайт.\n"
        "📌 **Правила:** от 2 до 20 символов, русские или английские буквы."
    )

@bot.on(events.NewMessage(pattern='/reset'))
async def handle_reset(event):
    if not event.is_private:
        return
    user_id = event.sender_id
    users_data = load_users()
    username = users_data["by_id"].get(str(user_id))

    if not username:
        await event.respond("Ты еще не зарегистрирован. Отправь логин для регистрации.")
        return

    new_password = generate_password()
    success = update_filebrowser_password(username, new_password)

    if success:
        save_user(user_id, username, new_password)
        await event.respond(
            "🔑 **Пароль успешно сброшен!**\n\n"
            f"🌐 **Сайт:** https://{DOMAIN}\n"
            f"👤 **Логин:** `{username}`\n"
            f"🔑 **Новый пароль:** `{new_password}`"
        )
    else:
        await event.respond("❌ Ошибка при сбросе пароля. Попробуйте чуть позже.")

@bot.on(events.NewMessage)
async def handle_text(event):
    if not event.is_private or event.message.media or event.raw_text.startswith('/'):
        return

    user_id = event.sender_id
    users_data = load_users()
    registered_username = users_data["by_id"].get(str(user_id))

    if registered_username:
        await event.respond(
            f"Твой личный диск уже готов!\n"
            f"👤 **Логин:** `{registered_username}`\n"
            f"🌐 **Сайт:** https://{DOMAIN}\n\n"
            f"Просто отправь мне файл для загрузки в хранилище."
        )
        return

    try:
        raw_login = event.raw_text
        username, error = sanitize_and_validate_username(raw_login)
        if error:
            await event.respond(f"❌ {error}")
            return

        img_path = os.path.join(DISKS_DIR, f"{username}.img")
        user_dir = os.path.join(FILES_DIR, username)

        if username.lower() in users_data["by_name"] or os.path.exists(img_path):
            await event.respond("⚠️ Этот логин уже занят. Попробуй другой вариант:")
            return

        create_user_virtual_disk(username, QUOTA_SIZE)
        password = generate_password()
        success, err_msg = create_filebrowser_user(username, password)

        if not success:
            await event.respond(f"❌ Не удалось создать пользователя. Ошибка: `{err_msg}`")
            return

        save_user(user_id, username, password)

        await event.respond(
            "🚀 **Твой персональный диск успешно выделен и подключен!**\n\n"
            f"🌐 **Адрес:** https://{DOMAIN}\n"
            f"👤 **Логин:** `{username}`\n"
            f"🔑 **Пароль:** `{password}`\n"
            f"💾 **Личный объем:** {QUOTA_SIZE}\n\n"
            "📌 **Как пользоваться:**\n"
            "1. Отправь мне сюда любой документ — скачивай его на ПК из папки `from_telegram`.\n"
            "2. Загрузи файл на сайте в папку `to_telegram` — и я мгновенно пришлю его сюда в чат!"
        )
    except Exception as ex:
        await event.respond(f"❌ Ошибка при создании диска: {ex}")

@bot.on(events.NewMessage)
async def handle_incoming_file(event):
    if not event.is_private or not event.message.media:
        return
    user_id = event.sender_id
    users_data = load_users()
    username = users_data["by_id"].get(str(user_id))
    if not username:
        await event.respond("Сначала отправь мне логин для регистрации.")
        return

    user_from = os.path.join(FILES_DIR, username, 'from_telegram')
    status_msg = await event.respond("⏳ Загружаю файл на твой диск...")
    try:
        path = await event.download_media(file=user_from)
        file_name = os.path.basename(path)
        await status_msg.edit(f"✅ Файл **{file_name}** доступен на сайте в папке `from_telegram`!")
    except OSError:
        await status_msg.edit(f"❌ Ошибка: на твоем диске закончилось место ({QUOTA_SIZE}).")

async def process_outgoing_file(file_path):
    prev_size = -1
    for _ in range(15):
        try:
            current_size = os.path.getsize(file_path)
            if current_size == prev_size and current_size > 0:
                break
            prev_size = current_size
            await asyncio.sleep(1)
        except FileNotFoundError:
            return

    parts = os.path.normpath(file_path).split(os.sep)
    try:
        u_index = parts.index('to_telegram') - 1
        username = parts[u_index]
    except Exception:
        return

    users_data = load_users()
    user_id = users_data["by_name"].get(username.lower())
    if not user_id:
        return

    file_name = os.path.basename(file_path)
    try:
        await bot.send_file(user_id, file_path, caption=f"📄 Файл с веб-диска: **{file_name}**")
        dest_path = os.path.join(FILES_DIR, username, 'from_telegram', file_name)
        if os.path.exists(dest_path):
            os.remove(dest_path)
        shutil.move(file_path, dest_path)
    except Exception as e:
        print(f"[{username}] Отправка отправки: {e}")

async def monitor_to_telegram_folders():
    while True:
        try:
            pattern = os.path.join(FILES_DIR, '*', 'to_telegram', '*')
            for file_path in glob.glob(pattern):
                if os.path.isfile(file_path):
                    await process_outgoing_file(file_path)
        except Exception:
            pass
        await asyncio.sleep(2)

async def main():
    global bot_username

    # 1. Сначала запускаем веб-сервер авторизации на порту 8085 (чтобы сайт открывался ВСЕГДА)
    app = web.Application()
    app.router.add_get('/login', web_login_page)
    app.router.add_get('/auth/new', web_new_session)
    app.router.add_get('/auth/qr/{session_id}.png', web_qr_image)
    app.router.add_get('/auth/status/{session_id}', web_check_status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8085)
    await site.start()
    print("Веб-сервис QR-авторизации мгновенно запущен на порту 8085!")

    # 2. Затем в фоновом режиме запускаем подключение к Telegram
    try:
        await bot.start(bot_token=BOT_TOKEN)
        me = await bot.get_me()
        bot_username = me.username
        print(f"Бот @{bot_username} успешно подключен к Telegram через WARP-прокси!")
        
        # Фоновый мониторинг файлов
        asyncio.create_task(monitor_to_telegram_folders())
    except Exception as e:
        print(f"[Warning] Не удалось подключиться к Telegram при старте: {e}")
        print("Веб-сервер продолжает работать, бот попытается переподключиться в фоне.")

    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
