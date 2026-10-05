import asyncio, csv, io, json, os, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, ChatMemberUpdated)

HERE = Path(__file__).parent


def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
CHANNEL_ID = os.environ["CHANNEL_ID"]
CHANNEL_LINK = os.environ["CHANNEL_LINK"]
S = json.loads((HERE / "settings.json").read_text(encoding="utf-8"))
B = S["buttons"]
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))  # часовой пояс для отложенных рассылок

db = sqlite3.connect(HERE / "users.db")
db.execute("""CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT, name TEXT, source TEXT,
    joined TEXT DEFAULT CURRENT_TIMESTAMP, lessons TEXT DEFAULT '', blocked INTEGER DEFAULT 0)""")
db.execute("""CREATE TABLE IF NOT EXISTS broadcasts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER, msg INTEGER, audience TEXT,
    btn_text TEXT, btn_url TEXT, send_at TEXT, status TEXT, ok INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0, total INTEGER DEFAULT 0)""")
db.commit()

dp = Dispatcher()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu():
    return kb([[InlineKeyboardButton(text=B["get"], callback_data="get")]])


async def is_subscribed(bot: Bot, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, uid)
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        print(f"[проверка подписки] не получилось для {uid}: {e!r}. Проверьте CHANNEL_ID и что бот админ канала", flush=True)
        return False


@dp.message(Command("whoami"))
async def whoami(m: Message):
    await m.answer(f"Ваш id: {m.from_user.id}\nАдмин в настройках: {ADMIN_ID}\nКанал в настройках: {CHANNEL_ID}\n"
                   + ("✅ Вы админ, откройте /admin" if admin(m) else "⚠️ Вы не админ: id не совпадает с ADMIN_ID"))


@dp.message(CommandStart())
async def start(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    source = parts[1] if len(parts) > 1 else ""
    db.execute("INSERT OR IGNORE INTO users(id,username,name,source) VALUES(?,?,?,?)",
               (m.from_user.id, m.from_user.username, m.from_user.full_name, source))
    db.execute("UPDATE users SET blocked=0 WHERE id=?", (m.from_user.id,))
    db.commit()
    await m.answer(S["welcome"].format(name=m.from_user.first_name), reply_markup=main_menu())


async def give(target: Message, uid: int):
    db.execute("UPDATE users SET lessons='получила доступ' WHERE id=?", (uid,))
    db.commit()
    await target.answer(S["lesson_done"], reply_markup=kb([
        [InlineKeyboardButton(text=B["channel"], url=CHANNEL_LINK)]]))


@dp.chat_member()
async def on_channel_member(ev: ChatMemberUpdated, bot: Bot):
    if str(ev.chat.id) != str(CHANNEL_ID) and f"@{ev.chat.username}".lower() != str(CHANNEL_ID).lower():
        return
    if ev.old_chat_member.status not in ("left", "kicked") or ev.new_chat_member.status != "member":
        return
    uid = ev.new_chat_member.user.id
    if not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        return
    try:
        await bot.send_message(uid, S["lesson_done"], reply_markup=kb([
            [InlineKeyboardButton(text=B["channel"], url=CHANNEL_LINK)]]))
        db.execute("UPDATE users SET lessons='получила доступ' WHERE id=?", (uid,))
        db.commit()
    except TelegramForbiddenError:
        db.execute("UPDATE users SET blocked=1 WHERE id=?", (uid,))
        db.commit()
    except Exception as e:
        print(f"[chat_member] не удалось написать {uid}: {e!r}", flush=True)


@dp.callback_query(F.data == "get")
async def get(c: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, c.from_user.id):
        await give(c.message, c.from_user.id)
    else:
        await c.message.answer(S["subscribe_needed"], reply_markup=kb([
            [InlineKeyboardButton(text=B["subscribe"], url=CHANNEL_LINK)],
            [InlineKeyboardButton(text=B["checked"], callback_data="check")]]))
    await c.answer()


@dp.callback_query(F.data == "check")
async def check(c: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, c.from_user.id):
        await give(c.message, c.from_user.id)
        await c.answer()
    else:
        await c.answer(S["not_subscribed"], show_alert=True)


@dp.callback_query(F.data == "menu")
async def menu(c: CallbackQuery):
    await c.message.answer(S["welcome"].format(name=c.from_user.first_name), reply_markup=main_menu())
    await c.answer()


def admin(m: Message):
    return ADMIN_ID and m.from_user.id == ADMIN_ID


bg_tasks = set()  # держим ссылки на фоновые задачи, иначе Python может их удалить


def spawn(coro):
    t = asyncio.create_task(coro)
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)
    return t


pending = {}  # состояние мастера рассылки у админа: шаг и выбранные параметры

AUD = {
    "all": ("Все", "1=1"),
    "new": ("Ещё не взяли доступ", "lessons=''"),
    "got": ("Уже взяли доступ", "lessons!=''"),
    "week": ("Новые за 7 дней", "joined>=datetime('now','-7 day')"),
}


def aud_where(a):
    if a.startswith("src:"):
        return "source=?", [a[4:]]
    return AUD[a][1], []


def aud_ids(a):
    w, p = aud_where(a)
    return [r[0] for r in db.execute(f"SELECT id FROM users WHERE blocked=0 AND {w}", p)]


def aud_name(a):
    return f"метка «{a[4:]}»" if a.startswith("src:") else AUD[a][0]


def btn_markup(text, url):
    return kb([[InlineKeyboardButton(text=text, url=url)]]) if text and url else None


def fmt_local(iso):
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def parse_time(text):
    """ДД.ММ.ГГГГ ЧЧ:ММ или ДД.ММ ЧЧ:ММ (по вашему времени). Возвращает время в UTC или None."""
    text = text.strip()
    for f in ("%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            d = datetime.strptime(text, f)
        except ValueError:
            continue
        if f == "%d.%m %H:%M":
            d = d.replace(year=datetime.now(TZ).year)
        return d.replace(tzinfo=TZ).astimezone(timezone.utc)
    return None


def admin_menu():
    return kb([
        [InlineKeyboardButton(text="📊 Статистика", callback_data="a:stats")],
        [InlineKeyboardButton(text="📣 Сделать рассылку", callback_data="a:send")],
        [InlineKeyboardButton(text="📅 Запланированные рассылки", callback_data="a:sched")],
        [InlineKeyboardButton(text="📥 Скачать базу (таблица)", callback_data="a:export")],
        [InlineKeyboardButton(text="⚙️ Редактировать тексты", callback_data="a:settings")],
        [InlineKeyboardButton(text="🔗 Ссылки на бота", callback_data="a:links")]])


def settings_menu():
    return kb([
        [InlineKeyboardButton(text="📝 Приветствие", callback_data="t:welcome")],
        [InlineKeyboardButton(text="📌 Нужна подписка", callback_data="t:subscribe_needed")],
        [InlineKeyboardButton(text="❌ Нет подписки", callback_data="t:not_subscribed")],
        [InlineKeyboardButton(text="✅ Готово (доступ открыт)", callback_data="t:lesson_done")],
        [InlineKeyboardButton(text="🔘 Кнопка: Забрать уроки", callback_data="t:btn_get")],
        [InlineKeyboardButton(text="🔘 Кнопка: Перейти в канал", callback_data="t:btn_channel")],
        [InlineKeyboardButton(text="🔘 Кнопка: Подписаться", callback_data="t:btn_subscribe")],
        [InlineKeyboardButton(text="🔘 Кнопка: Я подписалась", callback_data="t:btn_checked")],
        cancel_row()
    ])


def cancel_row():
    return [InlineKeyboardButton(text="Отмена", callback_data="a:cancel")]


@dp.message(Command("admin"))
async def admin_cmd(m: Message):
    if not admin(m):
        return
    pending.pop(m.from_user.id, None)
    await m.answer("Панель управления ботом 👇", reply_markup=admin_menu())


async def do_stats(target: Message):
    total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    week = db.execute("SELECT COUNT(*) FROM users WHERE joined>=datetime('now','-7 day')").fetchone()[0]
    got = db.execute("SELECT COUNT(*) FROM users WHERE lessons!=''").fetchone()[0]
    blocked = db.execute("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]
    await target.answer(f"👥 В базе: {total}\n🆕 За 7 дней: {week}\n🎁 Получили доступ в канал: {got}\n"
                        f"🚫 Заблокировали бота: {blocked}", reply_markup=admin_menu())


async def do_export(target: Message):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "username", "имя", "метка", "дата", "доступ", "заблокировал"])
    for r in db.execute("SELECT id,username,name,source,joined,lessons,blocked FROM users"):
        w.writerow(r)
    await target.answer_document(BufferedInputFile(out.getvalue().encode("utf-8-sig"), "baza.csv"))


def save_settings():
    """Сохраняет S в settings.json"""
    settings_file = HERE / "settings.json"
    settings_file.write_text(json.dumps(S, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_broadcast(bot: Bot, bid: int):
    row = db.execute("SELECT chat,msg,audience,btn_text,btn_url,status FROM broadcasts WHERE id=?", (bid,)).fetchone()
    if not row or row[5] not in ("scheduled", "now"):
        return
    chat, msg, aud, bt, bu, _ = row
    db.execute("UPDATE broadcasts SET status='sending' WHERE id=?", (bid,))
    db.commit()
    markup = btn_markup(bt, bu)
    ids = aud_ids(aud)
    ok = failed = 0
    for u in ids:
        for attempt in (1, 2):
            try:
                await bot.copy_message(u, chat, msg, reply_markup=markup)
                ok += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                continue
            except TelegramForbiddenError:
                db.execute("UPDATE users SET blocked=1 WHERE id=?", (u,))
                failed += 1
            except Exception:
                failed += 1
            break
        await asyncio.sleep(0.05)
    db.execute("UPDATE broadcasts SET status='done', ok=?, failed=?, total=? WHERE id=?", (ok, failed, len(ids), bid))
    db.commit()
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"✅ Рассылка №{bid} завершена ({aud_name(aud)}).\n"
                                   f"Доставлено: {ok} из {len(ids)}. Не дошло: {failed} (в основном заблокировали бота).",
                                   reply_markup=admin_menu())
        except Exception:
            pass


async def scheduler(bot: Bot):
    print("[планировщик] запущен", flush=True)
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()
            for (bid,) in db.execute("SELECT id FROM broadcasts WHERE status='scheduled' AND send_at<=?", (now,)).fetchall():
                await run_broadcast(bot, bid)
        except Exception as e:
            print("[планировщик]", repr(e), flush=True)
        await asyncio.sleep(30)


async def ask_audience(target: Message):
    rows = []
    for key, (title, _) in AUD.items():
        rows.append([InlineKeyboardButton(text=f"{title} ({len(aud_ids(key))})", callback_data=f"au:{key}")])
    for (src,) in db.execute("SELECT DISTINCT source FROM users WHERE source!='' AND blocked=0 LIMIT 8").fetchall():
        if len(src.encode()) <= 50:
            rows.append([InlineKeyboardButton(text=f"Метка {src} ({len(aud_ids('src:' + src))})",
                                              callback_data=f"au:src:{src}")])
    rows.append(cancel_row())
    await target.answer("Кому отправить?", reply_markup=kb(rows))


async def ask_when(target: Message):
    await target.answer("Когда отправить?", reply_markup=kb([
        [InlineKeyboardButton(text="🚀 Сейчас", callback_data="wh:now")],
        [InlineKeyboardButton(text="📅 Запланировать", callback_data="wh:later")],
        cancel_row()]))


async def show_confirm(m: Message, bot: Bot, st: dict):
    st["step"] = "confirm"
    await m.answer("Так увидят сообщение (ниже). Проверьте:")
    await bot.copy_message(m.chat.id, st["chat"], st["msg"], reply_markup=btn_markup(st.get("btn_text"), st.get("btn_url")))
    when = "сразу после подтверждения" if not st.get("send_at") else fmt_local(st["send_at"].isoformat()) + f" ({TZ.key})"
    await m.answer(f"Кому: {aud_name(st['aud'])}, {len(aud_ids(st['aud']))} чел.\nКнопка: "
                   f"{st.get('btn_text') or 'нет'}\nКогда: {when}",
                   reply_markup=kb([[InlineKeyboardButton(text="✅ Подтвердить", callback_data="a:go")], cancel_row()]))


def is_admin_cb(c: CallbackQuery):
    return bool(ADMIN_ID) and c.from_user.id == ADMIN_ID


@dp.callback_query(F.data == "a:settings")
async def settings_cmd(c: CallbackQuery):
    if not is_admin_cb(c):
        await c.answer()
        return
    pending.pop(c.from_user.id, None)
    await c.message.answer("Какой текст менять?", reply_markup=settings_menu())
    await c.answer()


@dp.callback_query(F.data.startswith("t:"))
async def choose_text(c: CallbackQuery):
    if not is_admin_cb(c):
        await c.answer()
        return
    key = c.data[2:]  # "t:welcome" → "welcome"
    st = pending.setdefault(c.from_user.id, {})
    st["step"] = "input_text"
    st["text_key"] = key

    # Показываем текущее значение
    if key.startswith("btn_"):
        btn_key = key[4:]  # "btn_get" → "get"
        current = S["buttons"].get(btn_key, "")
        title = f"Кнопка: {S['buttons'].get(btn_key, '')}"
    else:
        current = S.get(key, "")
        title = key

    await c.message.answer(
        f"Текущее значение для «{title}»:\n\n{current}\n\n" +
        f"Пришлите новый текст или нажмите «Отмена».",
        reply_markup=kb([cancel_row()]))
    await c.answer()


@dp.callback_query(F.data.startswith("a:"))
async def admin_buttons(c: CallbackQuery, bot: Bot):
    if not is_admin_cb(c):
        await c.answer()
        return
    act = c.data[2:]
    uid = c.from_user.id
    if act == "stats":
        await do_stats(c.message)
    elif act == "export":
        await do_export(c.message)
    elif act == "links":
        me = await bot.get_me()
        await c.message.answer(
            f"Ссылка на бота:\nhttps://t.me/{me.username}\n\n"
            f"С меткой (чтобы видеть, откуда пришли):\nhttps://t.me/{me.username}?start=insta\n"
            "Вместо insta можно написать любое слово латиницей. Метка видна в таблице и в выборе аудитории рассылки.",
            reply_markup=admin_menu())
    elif act == "send":
        pending[uid] = {"step": "wait"}
        await c.message.answer(
            "Пришлите сообщение для рассылки: текст, фото или видео. Ссылки можно вставлять прямо в текст.",
            reply_markup=kb([cancel_row()]))
    elif act == "sched":
        rows = db.execute("SELECT id,audience,send_at FROM broadcasts WHERE status='scheduled' ORDER BY send_at").fetchall()
        if not rows:
            await c.message.answer("Запланированных рассылок нет.", reply_markup=admin_menu())
        else:
            await c.message.answer("Запланированные рассылки (нажмите, чтобы отменить):", reply_markup=kb(
                [[InlineKeyboardButton(text=f"❌ №{i}: {fmt_local(t)}, {aud_name(a)}", callback_data=f"x:{i}")]
                 for i, a, t in rows]))
    elif act == "cancel":
        pending.pop(uid, None)
        await c.message.answer("Отменено.", reply_markup=admin_menu())
    elif act == "go":
        st = pending.pop(uid, None)
        if not st or st.get("step") != "confirm":
            await c.message.answer("Нечего отправлять.", reply_markup=admin_menu())
        else:
            send_at = st["send_at"].isoformat() if st.get("send_at") else None
            cur = db.execute(
                "INSERT INTO broadcasts(chat,msg,audience,btn_text,btn_url,send_at,status) VALUES(?,?,?,?,?,?,?)",
                (st["chat"], st["msg"], st["aud"], st.get("btn_text"), st.get("btn_url"), send_at,
                 "scheduled" if send_at else "now"))
            db.commit()
            if send_at:
                await c.message.answer(f"📅 Рассылка №{cur.lastrowid} запланирована на {fmt_local(send_at)}.",
                                       reply_markup=admin_menu())
            else:
                await c.message.answer("Отправляю… пришлю отчёт, когда закончу.")
                spawn(run_broadcast(bot, cur.lastrowid))
    await c.answer()


@dp.callback_query(F.data.startswith("x:"))
async def cancel_scheduled(c: CallbackQuery):
    if not is_admin_cb(c):
        await c.answer()
        return
    db.execute("UPDATE broadcasts SET status='cancelled' WHERE id=? AND status='scheduled'", (int(c.data[2:]),))
    db.commit()
    await c.message.answer("Рассылка отменена.", reply_markup=admin_menu())
    await c.answer()


@dp.callback_query(F.data.startswith("au:"))
async def choose_audience(c: CallbackQuery):
    st = pending.get(c.from_user.id)
    if not is_admin_cb(c) or not st or st["step"] != "aud":
        await c.answer()
        return
    st["aud"] = c.data[3:]
    st["step"] = "btn"
    await c.message.answer(
        "Добавить кнопку со ссылкой под сообщением? Пришлите одной строкой: текст кнопки | ссылка\n"
        "Например: Забрать скидку | https://t.me/lol623lol/12",
        reply_markup=kb([[InlineKeyboardButton(text="Без кнопки", callback_data="bt:none")], cancel_row()]))
    await c.answer()


@dp.callback_query(F.data == "bt:none")
async def no_button(c: CallbackQuery):
    st = pending.get(c.from_user.id)
    if not is_admin_cb(c) or not st or st["step"] != "btn":
        await c.answer()
        return
    st["step"] = "when"
    await ask_when(c.message)
    await c.answer()


@dp.callback_query(F.data.startswith("wh:"))
async def choose_when(c: CallbackQuery, bot: Bot):
    st = pending.get(c.from_user.id)
    if not is_admin_cb(c) or not st or st["step"] != "when":
        await c.answer()
        return
    if c.data == "wh:now":
        st["send_at"] = None
        await show_confirm(c.message, bot, st)
    else:
        st["step"] = "time"
        now = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
        await c.message.answer(f"Напишите дату и время по вашему времени ({TZ.key}), сейчас {now}.\n"
                               "Формат: 05.10 10:00", reply_markup=kb([cancel_row()]))
    await c.answer()


@dp.message(F.chat.type == "private", ~F.text.startswith("/"))
async def catch_admin_input(m: Message, bot: Bot):
    st = pending.get(m.from_user.id) if admin(m) else None
    if not st:
        return
    if st["step"] == "input_text":
        key = st["text_key"]
        new_text = m.text.strip()

        # Сохраняем в S
        if key.startswith("btn_"):
            btn_key = key[4:]
            S["buttons"][btn_key] = new_text
            title = f"Кнопка: {key[4:]}"
        else:
            S[key] = new_text
            title = key

        # Пишем в файл
        try:
            save_settings()
            await m.answer(f"✅ Сохранено: «{title}».", reply_markup=admin_menu())
            pending.pop(m.from_user.id, None)
        except Exception as e:
            await m.answer(f"❌ Ошибка сохранения: {e!r}", reply_markup=admin_menu())
            pending.pop(m.from_user.id, None)
        return
    if st["step"] == "wait":
        st.update(step="aud", chat=m.chat.id, msg=m.message_id)
        await ask_audience(m)
    elif st["step"] == "btn":
        text, _, url = (m.text or "").partition("|")
        text, url = text.strip(), url.strip()
        if not text or not url.startswith(("http://", "https://", "tg://")):
            await m.answer("Не поняла. Нужно так: текст кнопки | ссылка (ссылка начинается с https://). Или нажмите «Без кнопки».")
            return
        st.update(btn_text=text[:60], btn_url=url, step="when")
        await ask_when(m)
    elif st["step"] == "time":
        t = parse_time(m.text or "")
        if not t or t <= datetime.now(timezone.utc):
            await m.answer("Не поняла время или оно уже прошло. Формат: 05.10 10:00")
            return
        st["send_at"] = t
        await show_confirm(m, bot, st)


@dp.message(Command("stats"))
async def stats(m: Message):
    if admin(m):
        await do_stats(m)


@dp.message(Command("export"))
async def export(m: Message):
    if admin(m):
        await do_export(m)


async def main():
    bot = Bot(TOKEN)
    spawn(scheduler(bot))
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
