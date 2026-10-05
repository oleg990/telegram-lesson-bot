#!/bin/bash
# Установка бота на сервер (Debian/Ubuntu). Запускать от root: bash install.sh
set -e
DIR=${BOT_DIR:-/opt/tgbot}
SERVICE=${SERVICE:-tgbot}

if [ -e /etc/systemd/system/$SERVICE.service ] && [ -z "$NO_SERVICE" ]; then
  echo "Служба $SERVICE уже существует, ничего не трогаю. Остановка: сообщите Claude."; exit 1
fi
if [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ] && [ ! -f "$DIR/bot.py" ]; then
  echo "Папка $DIR уже занята чужими файлами, ничего не трогаю."; exit 1
fi

echo "== Проверяю Python (существующие программы на сервере не меняю) =="
if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  if command -v apt-get >/dev/null; then
    apt-get install -y python3-venv
  else
    echo "Нет python3-venv. Остановка."; exit 1
  fi
fi

mkdir -p "$DIR"
cd "$DIR"

# Код бота берём с GitHub, чтобы у каждого нового заказчика была последняя версия
REPO_RAW=${REPO_RAW:-https://raw.githubusercontent.com/oleg990/telegram-lesson-bot/main}

fetch() {  # fetch <файл в репозитории> <куда сохранить>
  if command -v curl >/dev/null; then
    curl -fsSL "$REPO_RAW/$1" -o "$2"
  elif command -v wget >/dev/null; then
    wget -qO "$2" "$REPO_RAW/$1"
  else
    echo "Нет ни curl, ни wget. Установите: apt-get install -y curl"; return 1
  fi
}

echo "== Скачиваю свежий код бота с GitHub =="
fetch bot.py bot.py.new || { rm -f bot.py.new; echo "Не удалось скачать bot.py. Проверьте интернет на сервере."; exit 1; }
if ! python3 -m py_compile bot.py.new; then
  rm -f bot.py.new; echo "Скачанный bot.py повреждён, ничего не меняю."; exit 1
fi
mv bot.py.new bot.py
rm -rf __pycache__

# Тексты заказчика не трогаем: settings.json скачиваем, только если его ещё нет
if [ ! -f settings.json ]; then
  fetch settings.json settings.json || { rm -f settings.json; echo "Не удалось скачать settings.json."; exit 1; }
fi

python3 -m venv venv
./venv/bin/pip install -q "aiogram>=3,<4" tzdata

if [ ! -f .env ]; then
  echo
  echo "== Настройки бота =="
  read -r -s -p "Токен бота от BotFather (при вводе не виден, это нормально): " T; echo
  read -r -p "Ваш числовой id (из @userinfobot): " A
  read -r -p "Канал: @my_channel, а для закрытого канала числовой номер вида -1001234567890: " C
  read -r -p "Ссылка на канал, например https://t.me/my_channel: " L
  umask 077
  printf 'BOT_TOKEN=%s\nADMIN_ID=%s\nCHANNEL_ID=%s\nCHANNEL_LINK=%s\n' "$T" "$A" "$C" "$L" > .env
  chmod 600 .env
fi

if [ -d /etc/systemd/system ] && [ -z "$NO_SERVICE" ]; then
cat > /etc/systemd/system/$SERVICE.service <<UNIT_EOF
[Unit]
Description=Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT_EOF
  systemctl daemon-reload
  systemctl enable --now $SERVICE
  sleep 3
  systemctl --no-pager status $SERVICE | head -8
  echo
  echo "Готово. Напишите боту /start в Telegram."
  echo "Если что-то не работает: journalctl -u $SERVICE -n 30 --no-pager"
fi
