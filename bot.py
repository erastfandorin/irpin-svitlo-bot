#!/usr/bin/env python3
"""Telegram-бот графіка відключень світла (ДТЕК Київські регіональні електромережі).

Запускається періодично (GitHub Actions cron). За один запуск:
  1. обробляє нові команди в боті (/start, /stop, /today, /tomorrow, /now);
  2. завантажує актуальний графік і надсилає графік на завтра, коли він з'являється
     або змінюється (а також повідомляє, якщо змінився графік на сьогодні);
  3. надсилає нагадування за 30 хв до відключення та за 30 хв до включення.

Без сторонніх залежностей: лише стандартна бібліотека Python 3.9+.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")
GROUP = os.environ.get("OUTAGE_GROUP", "GPV6.2")
GROUP_LABEL = GROUP.replace("GPV", "")
PLACE = os.environ.get("OUTAGE_PLACE", "Ірпінь")
DATA_URL = os.environ.get(
    "OUTAGE_DATA_URL",
    "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json",
)
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "state.json"))
WARN_BEFORE = timedelta(minutes=int(os.environ.get("WARN_BEFORE_MIN", "30")))
# Запуски йдуть приблизно раз на 5 хв (а GitHub cron буває із запізненням),
# тому нагадування, що мають прийти протягом найближчих LOOKAHEAD, чекаємо в цьому ж запуску.
LOOKAHEAD = timedelta(minutes=int(os.environ.get("LOOKAHEAD_MIN", "10")))

OFF, MAYBE, ON = "off", "maybe", "on"
# Значення погодинного слота → стан першої та другої половини години.
SLOT_MAP = {
    "yes": (ON, ON),
    "no": (OFF, OFF),
    "maybe": (MAYBE, MAYBE),
    "first": (OFF, ON),
    "second": (ON, OFF),
    "mfirst": (MAYBE, ON),
    "msecond": (ON, MAYBE),
}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]

DRY_RUN = False


def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- Telegram API

def tg(method, **params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задано")
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=int(params.get("timeout", 0)) + 20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "error_code": e.code, "description": str(e)}


def send(state, chat_id, text):
    if DRY_RUN:
        print(f"\n--- → {chat_id}\n{text}\n", flush=True)
        return True
    res = tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview="true")
    new_id = (res.get("parameters") or {}).get("migrate_to_chat_id")
    if new_id:  # групу перетворено на супергрупу — у неї новий id
        migrate(state, chat_id, new_id)
        return send(state, new_id, text)
    if not res.get("ok"):
        log("sendMessage failed:", chat_id, res.get("error_code"), res.get("description"))
        if res.get("error_code") == 403 and chat_id in state["subscribers"]:  # бота заблокували / видалили з групи
            state["subscribers"].remove(chat_id)
        return False
    return True


def migrate(state, old_id, new_id):
    subs = state["subscribers"]
    if old_id in subs:
        subs[subs.index(old_id)] = new_id
    log("chat migrated", old_id, "->", new_id)


def broadcast(state, text):
    for chat_id in list(state["subscribers"]):
        send(state, chat_id, text)


# ---------------------------------------------------------------- Графік

def fetch_data():
    req = urllib.request.Request(DATA_URL, headers={"User-Agent": "outage-schedule-bot", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def build_slots(data):
    """Повертає {date: [(start, end, state), ...48 півгодинних слотів]} для нашої черги."""
    days = {}
    for key, groups in (data.get("fact", {}).get("data") or {}).items():
        hours = groups.get(GROUP)
        if not hours:
            continue
        midnight = datetime.fromtimestamp(int(key), TZ)
        slots = []
        for h in range(1, 25):
            first, second = SLOT_MAP.get(hours.get(str(h), "yes"), (MAYBE, MAYBE))
            start = midnight + timedelta(hours=h - 1)
            mid = start + timedelta(minutes=30)
            slots.append((start, mid, first))
            slots.append((mid, start + timedelta(hours=1), second))
        days[midnight.date()] = slots
    return dict(sorted(days.items()))


def day_hash(slots):
    return hashlib.sha1("".join(s[2][0] for s in slots).encode()).hexdigest()[:12]


def merge(slots):
    """Склеює сусідні слоти з однаковим станом у інтервали [(start, end, state)]."""
    out = []
    for start, end, st in slots:
        if out and out[-1][2] == st and out[-1][1] == start:
            out[-1] = (out[-1][0], end, st)
        else:
            out.append((start, end, st))
    return out


def fmt_t(dt, day=None):
    if day is not None and dt.date() != day and dt.hour == 0 and dt.minute == 0:
        return "24:00"
    return dt.strftime("%H:%M")


def fmt_day(d):
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"


def fmt_hours(td):
    h = td.total_seconds() / 3600
    return (f"{h:.1f}".rstrip("0").rstrip(".")) + " год"


def schedule_text(day, slots, title, icon="🗓"):
    lines = [f"{icon} <b>{title}, {fmt_day(day)}</b>", f"Черга {GROUP_LABEL} · {PLACE}", ""]
    intervals = [iv for iv in merge(slots) if iv[2] != ON]
    if not intervals:
        lines.append("✅ Відключень не заплановано")
    else:
        for start, end, st in intervals:
            icon, note = ("🔴", "") if st == OFF else ("🟡", " (можливо)")
            lines.append(f"{icon} {fmt_t(start, day)}–{fmt_t(end, day)}{note}")
        off = sum(((e - s) for s, e, st in intervals if st == OFF), timedelta())
        maybe = sum(((e - s) for s, e, st in intervals if st == MAYBE), timedelta())
        lines.append("")
        summary = f"Без світла: {fmt_hours(off)}"
        if maybe:
            summary += f", можливо ще {fmt_hours(maybe)}"
        lines.append(summary)
    return "\n".join(lines)


def events(days):
    """Моменти переходів «є світло» ↔ «нема/можливо нема» по всьому відомому графіку."""
    timeline = [s for slots in days.values() for s in slots]
    evs = []
    for prev, cur in zip(timeline, timeline[1:]):
        if prev[1] != cur[0]:  # розрив у даних
            continue
        was_on, is_on = prev[2] == ON, cur[2] == ON
        if was_on and not is_on:
            evs.append({"type": "off", "at": cur[0], "maybe": cur[2] == MAYBE})
        elif not was_on and is_on:
            evs.append({"type": "on", "at": cur[0], "maybe": prev[2] == MAYBE})
    data_end = timeline[-1][1] if timeline else None
    return evs, data_end


def when(dt, now):
    t = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return f"о {t}"
    if dt.date() == now.date() + timedelta(days=1):
        return f"завтра о {t}"
    return f"{fmt_day(dt.date())} о {t}"


def warning_text(ev, evs, data_end, now):
    mins = max(1, round((ev["at"] - now).total_seconds() / 60))
    nxt = next((e for e in evs if e["at"] > ev["at"] and e["type"] != ev["type"]), None)
    if ev["type"] == "off":
        head = "Можливе відключення" if ev["maybe"] else "Відключення світла"
        text = f"⚠️ <b>{head} через {mins} хв</b> ({when(ev['at'], now)})"
        if nxt:
            text += f"\nСвітло мають дати {when(nxt['at'], now)} (через {fmt_hours(nxt['at'] - ev['at'])} після відключення)."
        elif data_end:
            text += "\nЧас включення поки невідомий."
        text += "\n🔋 Варто зарядити телефон і павербанк."
    else:
        text = f"💡 <b>Світло мають увімкнути через {mins} хв</b> ({when(ev['at'], now)})"
        if nxt:
            text += f"\nНаступне відключення: {when(nxt['at'], now)}."
        else:
            text += "\nДалі за графіком відключень не заплановано."
    return text


def status_text(days, now):
    evs, _ = events(days)
    cur = next((s for slots in days.values() for s in slots if s[0] <= now < s[1]), None)
    if not cur:
        return "Немає даних на поточний час."
    nxt = next((e for e in evs if e["at"] > now), None)
    if cur[2] == ON:
        text = "✅ Зараз за графіком світло є."
        if nxt:
            text += f"\nНаступне відключення: {when(nxt['at'], now)}."
    else:
        text = "🔴 Зараз за графіком світла немає." if cur[2] == OFF else "🟡 Зараз можливе відключення."
        if nxt:
            text += f"\nВключення: {when(nxt['at'], now)}."
    return text


# ---------------------------------------------------------------- Стан

def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("offset", 0)
    st.setdefault("subscribers", [])
    st.setdefault("days", {})       # date -> hash надісланого графіка
    st.setdefault("notified", {})   # ключ події -> ISO-час події
    return st


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- Логіка

HELP = (
    "Команди:\n"
    "/today — графік на сьогодні\n"
    "/tomorrow — графік на завтра\n"
    "/now — чи є зараз світло за графіком\n"
    "/stop — відписатися від сповіщень"
)


def day_message(days, day, title):
    if day in days:
        return schedule_text(day, days[day], title)
    return f"🗓 <b>{title}, {fmt_day(day)}</b>\nГрафік ще не опубліковано. Надішлю, щойно з'явиться."


def subscribe(state, chat_id, days, now, group=False):
    if chat_id not in state["subscribers"]:
        state["subscribers"].append(chat_id)
    where = "для цієї групи" if group else ""
    send(state, chat_id,
         f"👋 Підписка оформлена{(' ' + where) if where else ''}: черга <b>{GROUP_LABEL}</b>, {PLACE}.\n\n"
         f"Я надсилатиму графік на завтра, щойно його опублікують, і нагадуватиму "
         f"за 30 хв до відключення та за 30 хв до включення.\n\n{HELP}")
    send(state, chat_id, day_message(days, now.date(), "Сьогодні"))
    tomorrow = now.date() + timedelta(days=1)
    if tomorrow in days:
        send(state, chat_id, day_message(days, tomorrow, "Графік на завтра"))


def unsubscribe(state, chat_id):
    if chat_id in state["subscribers"]:
        state["subscribers"].remove(chat_id)


COMMANDS = {"/start", "/stop", "/today", "/tomorrow", "/now", "/help"}


def handle_member_update(state, days, now, upd):
    """Бота додали в групу / видалили з неї / заблокували в приваті."""
    chat = upd.get("chat") or {}
    chat_id, is_group = chat.get("id"), chat.get("type") in ("group", "supergroup")
    old = (upd.get("old_chat_member") or {}).get("status")
    new = (upd.get("new_chat_member") or {}).get("status")
    log("membership", chat.get("type"), old, "->", new)
    if new in ("left", "kicked"):
        unsubscribe(state, chat_id)
    elif is_group and new in ("member", "administrator") and old in ("left", "kicked", None):
        subscribe(state, chat_id, days, now, group=True)


def handle_updates(state, days, now, poll=0):
    """poll > 0 — long polling: чекає нових повідомлень до poll секунд і відповідає одразу."""
    res = tg("getUpdates", offset=state["offset"], timeout=poll, allowed_updates=["message", "my_chat_member"])
    if not res.get("ok"):
        log("getUpdates failed:", res.get("description"))
        return
    if not state.get("bot_username"):
        me = tg("getMe")
        state["bot_username"] = (me.get("result") or {}).get("username")
        log(f"bot=@{state['bot_username']}")
    if res["result"]:
        log(f"updates={len(res['result'])}")
    for upd in res["result"]:
        state["offset"] = upd["update_id"] + 1
        if upd.get("my_chat_member"):
            handle_member_update(state, days, now, upd["my_chat_member"])
            continue
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id, is_group = chat.get("id"), chat.get("type") in ("group", "supergroup")
        if msg.get("migrate_to_chat_id"):
            migrate(state, chat_id, msg["migrate_to_chat_id"])
            continue
        text = (msg.get("text") or "").strip()
        if not chat_id or not text.startswith("/"):
            continue
        cmd, _, addressee = text.split()[0].lower().partition("@")
        me = (state.get("bot_username") or "").lower()
        if addressee and me and addressee != me:
            continue  # команда для іншого бота в групі
        if is_group and cmd not in COMMANDS:
            continue  # у групі не реагуємо на чужі команди
        today, tomorrow = now.date(), now.date() + timedelta(days=1)
        if cmd == "/start":
            subscribe(state, chat_id, days, now, group=is_group)
        elif cmd == "/stop":
            unsubscribe(state, chat_id)
            send(state, chat_id, "🔕 Сповіщення вимкнено. /start — увімкнути знову.")
        elif cmd == "/today":
            send(state, chat_id, day_message(days, today, "Сьогодні"))
        elif cmd == "/tomorrow":
            send(state, chat_id, day_message(days, tomorrow, "Графік на завтра"))
        elif cmd == "/now":
            send(state, chat_id, status_text(days, now))
        else:
            send(state, chat_id, HELP)


def check_schedule_changes(state, days, now):
    today, tomorrow = now.date(), now.date() + timedelta(days=1)
    for day, title, changed_title in (
        (today, None, "Графік на сьогодні змінився"),
        (tomorrow, "Графік на завтра", "Оновлено графік на завтра"),
    ):
        if day not in days:
            continue
        key, h = day.isoformat(), day_hash(days[day])
        old = state["days"].get(key)
        if old == h:
            continue
        if old is None and title:
            log("new schedule for", key)
            broadcast(state, schedule_text(day, days[day], title))
        elif old is not None:
            log("schedule changed for", key)
            broadcast(state, schedule_text(day, days[day], changed_title, icon="🔄"))
        state["days"][key] = h
    # прибираємо старі дні
    for key in list(state["days"]):
        if key < (today - timedelta(days=2)).isoformat():
            del state["days"][key]


def due_warnings(state, days, now, lookahead=LOOKAHEAD):
    evs, data_end = events(days)
    due = []
    for ev in evs:
        key = f"{ev['type']}:{ev['at'].isoformat()}"
        notify_at = ev["at"] - WARN_BEFORE
        if key in state["notified"] or ev["at"] <= now or notify_at > now + lookahead:
            continue
        due.append((notify_at, key, ev))
    return sorted(due, key=lambda x: x[0]), evs, data_end


def next_warning_at(state, days, now):
    """Найближчий момент, коли треба надіслати нагадування (або None)."""
    due, _, _ = due_warnings(state, days, now, lookahead=timedelta(days=3))
    return due[0][0] if due else None


def send_warnings(state, days, now_fn, sleep=True, lookahead=LOOKAHEAD):
    due, evs, data_end = due_warnings(state, days, now_fn(), lookahead)
    for notify_at, key, ev in due:
        wait = (notify_at - now_fn()).total_seconds()
        if wait > 0:
            if not sleep:
                continue
            log(f"waiting {int(wait)}s for {key}")
            time.sleep(wait)
        now = now_fn()
        if ev["at"] <= now:
            continue
        log("warning", key)
        broadcast(state, warning_text(ev, evs, data_end, now))
        state["notified"][key] = ev["at"].isoformat()
        save_state(state)
    cutoff = (now_fn() - timedelta(days=2)).isoformat()
    state["notified"] = {k: v for k, v in state["notified"].items() if v >= cutoff}


DATA_TTL = timedelta(minutes=int(os.environ.get("DATA_TTL_MIN", "5")))
MAX_POLL = 25  # секунд одного long-poll запиту


def run_loop(state, days, now_fn, duration):
    """Безперервна робота: відповідає на команди одразу, графік оновлює раз на DATA_TTL,
    нагадування надсилає точно в момент «за 30 хв»."""
    deadline = now_fn() + duration
    fetched_at = now_fn()
    check_schedule_changes(state, days, now_fn())
    save_state(state)
    log(f"loop started until {deadline:%H:%M}")
    while now_fn() < deadline:
        try:
            if now_fn() - fetched_at >= DATA_TTL:
                fetched_at = now_fn()
                try:
                    fresh = build_slots(fetch_data())
                    if fresh:
                        days = fresh
                        check_schedule_changes(state, days, now_fn())
                except Exception as e:  # дані недоступні — працюємо зі старими
                    log("fetch failed:", e)
            send_warnings(state, days, now_fn, sleep=False, lookahead=timedelta(0))
            now = now_fn()
            wake = [deadline, fetched_at + DATA_TTL]
            nw = next_warning_at(state, days, now)
            if nw:
                wake.append(nw)
            poll = math.ceil(max(0, min(MAX_POLL, (min(wake) - now).total_seconds())))
            before = (state["offset"], list(state["subscribers"]))
            handle_updates(state, days, now, poll=poll)
            if (state["offset"], state["subscribers"]) != before:
                save_state(state)
        except Exception as e:
            log("loop error:", repr(e))
            time.sleep(5)
    save_state(state)
    log(f"loop finished: subscribers={len(state['subscribers'])}")
    return 0


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="друкувати повідомлення замість надсилання")
    ap.add_argument("--now", help="імітувати поточний час, напр. 2026-11-27T06:00 (лише з --dry-run)")
    ap.add_argument("--preview", action="store_true", help="лише показати графік і події")
    ap.add_argument("--loop", type=float, metavar="MIN",
                    help="працювати безперервно MIN хвилин: миттєві відповіді на команди (long polling)")
    args = ap.parse_args()
    DRY_RUN = args.dry_run or args.preview

    if args.now:
        fake = datetime.fromisoformat(args.now).replace(tzinfo=TZ)
        offset = fake - datetime.now(TZ)
        now_fn = lambda: datetime.now(TZ) + offset  # noqa: E731
    else:
        now_fn = lambda: datetime.now(TZ)  # noqa: E731

    days = build_slots(fetch_data())
    if not days:
        log(f"немає даних для {GROUP}")
        return 1
    now = now_fn()

    if args.preview:
        for d, slots in days.items():
            print(schedule_text(d, slots, "Графік"), "\n")
        print(status_text(days, now), "\n")
        evs, data_end = events(days)
        for ev in evs:
            if ev["at"] > now:
                print(ev["type"], ev["at"].strftime("%d.%m %H:%M"), "(можливо)" if ev["maybe"] else "")
        return 0

    state = load_state()
    if args.loop:
        return run_loop(state, days, now_fn, timedelta(minutes=args.loop))
    if args.dry_run and not state["subscribers"]:
        state["subscribers"] = ["dry-run"]
    if not DRY_RUN:
        handle_updates(state, days, now)
    check_schedule_changes(state, days, now)
    save_state(state)
    send_warnings(state, days, now_fn, sleep=not args.dry_run)
    if not DRY_RUN:
        handle_updates(state, days, now_fn())  # відповісти на команди, що прийшли під час очікування
    save_state(state)
    log(f"ok: subscribers={len(state['subscribers'])} days={list(state['days'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
