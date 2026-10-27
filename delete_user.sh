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

echo "=== Удаление пользователя [$USER] ==="

# 1. Удаление из веб-панели FileBrowser
systemctl stop filebrowser
/usr/local/bin/filebrowser users rm "$USER" -d "$DB_PATH" 2>/dev/null
systemctl start filebrowser
echo "[+] Пользователь удален из веб-панели"

# 2. Размонтирование виртуального диска
if mountpoint -q "$MOUNT_DIR"; then
    umount -l "$MOUNT_DIR"
    echo "[+] Диск размонтирован"
fi

# 3. Удаление образа диска и папки
rm -f "$IMG_FILE"
rm -rf "$MOUNT_DIR"
echo "[+] Файлы и виртуальный диск удалены с сервера"

# 4. Удаление из users.json без учета регистра
if [ -f "$USERS_JSON" ]; then
    python3 -c "
import json
with open('$USERS_JSON', 'r') as f:
    data = json.load(f)
target = '$USER'.strip().lower()
uid = data.get('by_name', {}).pop(target, None)
if not uid:
    for k, v in list(data.get('by_id', {}).items()):
        if v.lower() == target:
            uid = k
            break
if uid:
    data.get('by_id', {}).pop(str(uid), None)
    data['by_name'] = {k: v for k, v in data.get('by_name', {}).items() if str(v) != str(uid)}
with open('$USERS_JSON', 'w') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
"
    echo "[+] Привязка Telegram ID удалена"
fi

echo "=== Пользователь [$USER] полностью стерт из системы! ==="
