# 🌐 Архитектура и развертывание FileShareService (Hybrid RU + EU)

Персональное облачное хранилище с Telegram-ботом, авторизацией по динамическому QR-коду и изоляцией пользователей на уровне виртуальных дисков Linux (ext4 loop devices).

---

## 🏛 Схема архитектуры

```text
[ Пользователь в РФ / Вуз ]
            │
            ▼ (HTTPS, порт 443 — пинг ~5 мс, без блокировок)
┌─────────────────────────────────────────────────────────────┐
│ ФРОНТЕНД-УЗЕЛ: Россия (Москва)                              │
│ • Nginx (Reverse Proxy + Let's Encrypt SSL)                 │
│ • Защита от бесконечного редиректа при выходе               │
│ • Служба tg-tunnel.service (Постоянный защищенный SSH-канал)│
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼ (Шифрованный TCP-туннель: SSH Bastion)
┌─────────────────────────────────────────────────────────────┐
│ БЭКЕНД-УЗЕЛ: Нидерланды (Амстердам)                         │
│ • Изолированный пользователь tunneluser (без доступа к shell)│
│ • FileBrowser (порт 8080 — локальный диск)                  │
│ • Telegram Bot + QR Web API (порт 8085 на aiohttp)          │
│ • Квоты 1GB через Sparse-образы ext4 (/opt/tg-cloud/disks/) │
│ • Cloudflare WARP (SOCKS5 127.0.0.1:40000 к Telegram API)  │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼ (Локальный SOCKS5)
                 [ Серверы Telegram ]
```

---

## 📋 Требования
1. **Сервер №1 (Фронтенд):** VPS в России.
2. **Сервер №2 (Бэкенд):** VPS в Европе.
3. **Домен:** A-запись домена указывает на **IP-адрес Фронтенда (RU)** в режиме Proxied.
4. **Telegram:** `API_ID`, `API_HASH` ([my.telegram.org](https://my.telegram.org/)) и токен от `@BotFather`.

---

# ЧАСТЬ 1. Развертывание БЭКЕНДА

Подключитесь по SSH к EU серверу под `root`.

### 1.1. Базовая подготовка и Swap
```bash
apt update && apt upgrade -y
apt install -y curl python3-pip python3-venv e2fsprogs

# Создание Swap-файла 1 ГБ для защиты от OOM
if [ $(swapon --show | wc -l) -eq 0 ]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Каталоги проекта
mkdir -p /opt/tg-cloud/files
mkdir -p /opt/tg-cloud/disks
cd /opt/tg-cloud

# Python виртуальное окружение
python3 -m venv /opt/tg-cloud/venv
/opt/tg-cloud/venv/bin/pip install telethon aiohttp qrcode[pil] PySocks "python-socks[asyncio]"
```

### 1.2. Обход сетевых аномалий к Telegram (Cloudflare WARP)
В европейских дата-центрах локальные маршруты к подсетям Telegram могут сбрасываться. Локальный SOCKS5-прокси решает это проблему:

```bash
# Установка официального WARP
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ noble main" | tee /etc/apt/sources.list.d/cloudflare-client.list
apt update && apt install -y cloudflare-warp

# Настройка в режиме локального SOCKS5 на порту 40000
warp-cli --accept-tos registration new 2>/dev/null || warp-cli registration new
warp-cli --accept-tos mode proxy 2>/dev/null || warp-cli mode proxy
warp-cli --accept-tos proxy port 40000 2>/dev/null || warp-cli proxy port 40000
warp-cli --accept-tos connect 2>/dev/null || warp-cli connect
systemctl enable --now warp-svc
```

### 1.3. Установка FileBrowser
```bash
curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash

cd /opt/tg-cloud
filebrowser config init -d /opt/tg-cloud/filebrowser.db
filebrowser config set -d /opt/tg-cloud/filebrowser.db --root /opt/tg-cloud/files --address 127.0.0.1 --port 8080

# Создание администратора (пароль строго от 12 символов!)
filebrowser users add admin SuperSecretAdminPass123 --scope / --perm.admin=true -d /opt/tg-cloud/filebrowser.db
filebrowser rules add 'lost\+found' -d /opt/tg-cloud/filebrowser.db
```

### 1.4. Вспомогательные скрипты бэкенда

#### Скрипт автомонтирования дисков при старте системы:
```bash
cat << 'EOF' > /opt/tg-cloud/mount_disks.sh
#!/bin/bash
for img in /opt/tg-cloud/disks/*.img; do
    [ -f "$img" ] || continue
    user=$(basename "$img" .img)
    mount_dir="/opt/tg-cloud/files/$user"
    mkdir -p "$mount_dir"
    if ! mountpoint -q "$mount_dir"; then
        mount -o loop "$img" "$mount_dir"
    fi
done
EOF
chmod +x /opt/tg-cloud/mount_disks.sh
```

#### Скрипт полного удаления пользователя:
```bash
cat << 'EOF' > /opt/tg-cloud/delete_user.sh
#!/bin/bash
if [ -z "$1" ]; then
    echo "Использование: ./delete_user.sh <логин>"
    exit 1
fi
USER="$1"
MOUNT_DIR="/opt/tg-cloud/files/$USER"
IMG_FILE="/opt/tg-cloud/disks/$USER.img"
DB_PATH="/opt/tg-cloud/filebrowser.db"
USERS_JSON="/opt/tg-cloud/users.json"

systemctl stop filebrowser
/usr/local/bin/filebrowser users rm "$USER" -d "$DB_PATH" 2>/dev/null
systemctl start filebrowser

if mountpoint -q "$MOUNT_DIR"; then
    umount -l "$MOUNT_DIR"
fi
rm -f "$IMG_FILE"
rm -rf "$MOUNT_DIR"

if [ -f "$USERS_JSON" ]; then
    python3 -c "
import json
with open('$USERS_JSON', 'r') as f: data = json.load(f)
target = '$USER'.strip().lower()
uid = data.get('by_name', {}).pop(target, None)
if not uid:
    for k, v in list(data.get('by_id', {}).items()):
        if v.lower() == target: uid = k; break
if uid:
    data.get('by_id', {}).pop(str(uid), None)
    data['by_name'] = {k: v for k, v in data.get('by_name', {}).items() if str(v) != str(uid)}
with open('$USERS_JSON', 'w') as f: json.dump(data, f, indent=2, ensure_ascii=False)
"
fi
echo "[+] Пользователь [$USER] полностью стерт."
EOF
chmod +x /opt/tg-cloud/delete_user.sh
```

### 1.5. Код бота и QR-сервера (`/opt/tg-cloud/bot.py`)
> **Ключевые архитектурные решения в коде:**
> 1. `app = web.Application()` запускается **самым первым**, чтобы веб-страница QR-входа на порту 8085 работала всегда, независимо от задержек сети Telegram.
> 2. `proxy=(socks.SOCKS5, '127.0.0.1', 40000)` подключает Telethon через локальный WARP.
> 3. `generate_password(length=12)` предотвращает ошибки коротких паролей FileBrowser.
> 4. `unicodedata.normalize('NFC', ...)` обеспечивает безопасную поддержку русских логинов без инъекций.

*(Создайте файл `/opt/tg-cloud/bot.py`, вставьте проверенный код из проекта и укажите ваши `API_ID`, `API_HASH`, `BOT_TOKEN` и `DOMAIN`).*

Инициализируйте пустую базу пользователей:
```bash
echo '{"by_id": {}, "by_name": {}, "passwords": {}}' > /opt/tg-cloud/users.json
```

### 1.6. Настройка systemd служб бэкенда

```bash
# 1. Служба автомонтирования
cat << 'EOF' > /etc/systemd/system/tg-mounts.service
[Unit]
Description=Mount User Virtual Disks
Before=filebrowser.service tg-sync.service
After=local-fs.target
[Service]
Type=oneshot
ExecStart=/opt/tg-cloud/mount_disks.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF

# 2. Служба FileBrowser
cat << 'EOF' > /etc/systemd/system/filebrowser.service
[Unit]
Description=FileBrowser
After=network.target tg-mounts.service
[Service]
User=root
ExecStart=/usr/local/bin/filebrowser -d /opt/tg-cloud/filebrowser.db
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

# 3. Служба бота
cat << 'EOF' > /etc/systemd/system/tg-sync.service
[Unit]
Description=Telegram Bot Cloud Service
After=network.target tg-mounts.service filebrowser.service warp-svc.service
[Service]
User=root
WorkingDirectory=/opt/tg-cloud
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/tg-cloud/venv/bin/python3 /opt/tg-cloud/bot.py
Restart=always
RestartSec=15
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now tg-mounts filebrowser tg-sync
```

### 1.7. Изоляция и безопасность туннеля
Создаем бесправного пользователя, у которого **нет доступа к командной строке**, и разрешаем ему пробрасывать **только порты 8080 и 8085**:

```bash
useradd -m -d /home/tunneluser -s /usr/sbin/nologin tunneluser
mkdir -p /home/tunneluser/.ssh
chmod 700 /home/tunneluser/.ssh
```

---

# ЧАСТЬ 2. Развертывание ФРОНТЕНДА

Подключитесь по SSH к RU серверу под `root`.

### 2.1. Установка Nginx и Certbot
```bash
apt update && apt upgrade -y
apt install -y nginx certbot python3-certbot-nginx
```

### 2.2. Генерация ключа и привязка к EU
```bash
# 1. Генерация Ed25519 ключа на фронтенде
ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519

# 2. Выводим публичный ключ
cat /root/.ssh/id_ed25519.pub
```

#### Скопируйте эту строчку и выполните на сервере в EU:
```bash
# Замените ВАШ_КЛЮЧ на строку с RU сервера (строго в одну строчку!)
echo 'no-pty,no-X11-forwarding,no-agent-forwarding,permitopen="127.0.0.1:8080",permitopen="127.0.0.1:8085" ssh-ed25519 ВАШ_КЛЮЧ' > /home/tunneluser/.ssh/authorized_keys

chown -R tunneluser:tunneluser /home/tunneluser
chmod 750 /home/tunneluser
chmod 700 /home/tunneluser/.ssh
chmod 600 /home/tunneluser/.ssh/authorized_keys
```

### 2.3. Создание системной службы постоянного SSH-туннеля в RU
На сервере в **RU**:
```bash
cat << 'EOF' > /etc/systemd/system/tg-tunnel.service
[Unit]
Description=Secure SSH Tunnel to EU Backend
After=network.target

[Service]
User=root
# Замените IP_EU на реальный IP вашего европейского сервера
ExecStart=/usr/bin/ssh -NT -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -L 127.0.0.1:8080:127.0.0.1:8080 -L 127.0.0.1:8085:127.0.0.1:8085 tunneluser@IP_EU
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now tg-tunnel
```
*(Проверьте: `systemctl status tg-tunnel` должен быть `active (running)`)*.

### 2.4. Выпуск SSL и настройка Nginx (RU)

#### Этап А: Временный конфиг для Certbot
```bash
rm -f /etc/nginx/sites-enabled/default

cat << 'EOF' > /etc/nginx/sites-available/tg-cloud
server {
    listen 80;
    listen [::]:80;
    server_name devopslymar.ru;

    location / {
        return 200 "OK";
    }
}
EOF

ln -sf /etc/nginx/sites-available/tg-cloud /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# Выпуск сертификата Let's Encrypt
certbot --nginx -d devopslymar.ru
```

#### Этап Б: Боевой конфиг Nginx
> **Важное решение:** В блок `location /` встроен безопасный JavaScript-перехватчик с защитой от рекурсии (`var r = false`), который мгновенно открывает QR-страницу `/login` при выходе, не зацикливая браузер.

```bash
cat << 'EOF' > /etc/nginx/sites-available/tg-cloud
server {
    listen 80;
    listen [::]:80;
    server_name devopslymar.ru;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name devopslymar.ru;

    ssl_certificate /etc/letsencrypt/live/devopslymar.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/devopslymar.ru/privkey.pem;

    client_max_body_size 2048M;
    client_body_timeout 300s;

    # Страница входа с QR-кодом (через туннель в Амстердам на 8085)
    location = /login {
        proxy_pass http://127.0.0.1:8085/login;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # API проверки сессий
    location /auth/ {
        proxy_pass http://127.0.0.1:8085/auth/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Хранилище файлов (FileBrowser через туннель в Амстердам на 8080)
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;

        proxy_set_header Accept-Encoding "";
        # Безопасный перехват выхода без бесконечного редиректа
        sub_filter '</body>' '<script>(function(){var r=false;setInterval(function(){if(!r&&window.location.pathname==="/login"){r=true;window.location.replace("/login");}},400);document.addEventListener("click",function(e){var el=e.target.closest("button, a, div");if(el&&/Выйти|Logout/i.test(el.textContent)){r=true;setTimeout(function(){window.location.replace("/login");},100);}});})();</script></body>';
        sub_filter_once on;
    }
}
EOF

nginx -t && systemctl reload nginx
```

---

## 🎯 Проверка работы

1. **В браузере:** Перейдите на `https://devopslymar.ru/login`. Страница открывается моментально из Москвы со статусом `200 OK`.
2. **В Telegram:** Напишите боту `/start`, зарегистрируйте логин (создастся персональный диск 1 ГБ).
3. **QR-вход:** Наведите камеру телефона на монитор ➜ нажмите «Открыть в Telegram» ➜ бот одобрит вход ➜ страница сама войдет в ваше хранилище!
4. **Безопасный выход:** Нажмите «Выйти» в левом меню ➜ страница мгновенно и чисто вернется на экран с QR-кодом.
