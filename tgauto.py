# -*- coding: utf-8 -*-
"""tgauto — мультиаккаунт Telegram платформа: дискавери каналов, парсинг, рассылка.

Один файл, Telethon. Читает СВОЙ accounts.json (формат ниже) — живые сессии
voice-bot/tg_multi не трогает.

accounts.json:
  [{"name":"acc1", "proxy":"user:pass@host:port",
    "session_string":"<telethon StringSession>"     # ИЛИ:
    "session":"/root/tg-acc-us"}]                    # путь к .session (без .session тоже ок)

Команды:
  python tgauto.py selfcheck                         # оффлайн-проверка логики
  python tgauto.py validate                          # какие аккаунты живы (get_me)
  python tgauto.py discover -k qa jobs --depth 2     # поиск каналов по словам + похожие
  python tgauto.py parse --limit 500                 # спарсить сообщения каналов из БД
  python tgauto.py parse --members                   # + участников (где видно)
  python tgauto.py send send.json                    # рассылка по задачам

Premium-аккаунт в discover автоматически даёт более глубокий список похожих каналов
(getChannelRecommendations отдаёт premium-аккаунтам расширенную выдачу).
"""
import argparse
import asyncio
import hashlib
import html
import json
import random
import re
import sqlite3
import time
import urllib.request
from collections import Counter
from pathlib import Path

import socks
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import SearchRequest, ImportContactsRequest, DeleteContactsRequest
from telethon.tl.functions.channels import (
    GetChannelRecommendationsRequest, GetFullChannelRequest, GetParticipantsRequest)
from telethon.tl.types import ChannelParticipantsAdmins, InputPhoneContact
from telethon.tl.functions.stories import (
    GetPeerStoriesRequest, ReadStoriesRequest, IncrementStoryViewsRequest, SendReactionRequest)
from telethon.tl.functions.messages import SendReactionRequest as MsgSendReaction
from telethon.tl.functions.payments import (
    GetStarGiftsRequest, GetPaymentFormRequest, SendStarsFormRequest, GetStarsStatusRequest)
from telethon.tl.types import (
    Channel, ReactionEmoji, InputInvoiceStarGift, InputPeerSelf, TextWithEntities, PeerUser)
from telethon.errors import FloodWaitError, PeerFloodError, UserPrivacyRestrictedError

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
BASE = Path(__file__).resolve().parent
DB = BASE / "data.db"
ACCOUNTS = BASE / "accounts.json"

# ── core: proxy / accounts / clients / db ─────────────────────────────────────

def parse_proxy(p):
    """user:pass@host:port  ->  PySocks SOCKS5 tuple (Telethon proxy=)."""
    p = (p or "").strip()
    if not p:
        return None
    if "://" in p:
        p = p.split("://", 1)[1]
    if "@" not in p:
        return None
    cred, hp = p.rsplit("@", 1)
    user, _, pw = cred.partition(":")
    host, _, port = hp.rpartition(":")
    return (socks.SOCKS5, host, int(port), True, user, pw)


def load_accounts(path=ACCOUNTS):
    accs = json.loads(Path(path).read_text(encoding="utf-8"))
    accs = accs if isinstance(accs, list) else list(accs.values())
    if not accs:
        raise SystemExit(f"нет аккаунтов в {path} — заполни файл (см. шапку tgauto.py)")
    return accs


def search_account():
    """Премиум-аккаунт для поиска/дискавери (role=search). Fallback — первый."""
    accs = load_accounts()
    for a in accs:
        if a.get("role") == "search":
            return a
    return accs[0]


def work_accounts():
    """Рабочие аккаунты (без премиум-поисковика и замороженных)."""
    accs = [a for a in load_accounts() if a.get("role") not in ("search", "frozen", "gift", "dead")]
    return accs or load_accounts()


def split_buckets(targets, accs):
    """Раздать цели аккаунтам СТАБИЛЬНО (rendezvous hashing).

    Раньше было `buckets[i % len(accs)]` — при заморозке одного аккаунта менялось
    len(accs) и ВСЯ раскладка съезжала: каждый аккаунт получал чужие каналы, кеш
    access_hash промахивался, шёл массовый ResolveUsername -> FloodWait 12ч у всех.
    Здесь канал закреплён за аккаунтом по хешу пары (цель, имя аккаунта), поэтому
    выпадение аккаунта перераспределяет только ЕГО каналы.
    """
    buckets = [[] for _ in accs]
    idx = {a["name"]: i for i, a in enumerate(accs)}
    for t in targets:
        owner = max(accs, key=lambda a: hashlib.md5(f"{t}|{a['name']}".encode()).digest())
        buckets[idx[owner["name"]]].append(t)
    return buckets


def target_usernames(con, cls=None, limit=None):
    """Список @username каналов из БД, опционально по классу (it_blogger и т.п.)."""
    q = "select username from channels where username is not null"
    p = []
    if cls:
        q += " and cls=?"; p.append(cls)
    q += " order by participants desc"
    if limit:
        q += " limit ?"; p.append(limit)
    return [r[0] for r in con.execute(q, p).fetchall()]


# ── governor: анти-фриз (стадии, дневные лимиты, человеческие паузы) ──────────
# Дневные лимиты действий НА АККАУНТ. Свежие (warming) - мизер, растёт с возрастом.
CAPS = {
    # warming (первые дни) = ТОЛЬКО пассив: просмотры/чтение, БЕЗ реакций (day-0 реакции и морозят)
    "warming": {"story_view": 15, "story_react": 0, "post_react": 0, "join": 1,
                "dm": 0, "comment": 0, "profile": 1, "day": 18},
    "ramping": {"story_view": 40, "story_react": 10, "post_react": 20, "join": 4,
                "dm": 5, "comment": 5, "profile": 1, "day": 55},
    "active":  {"story_view": 80, "story_react": 25, "post_react": 45, "join": 8,
                "dm": 20, "comment": 15, "profile": 2, "day": 120},
}
DELAY = {"warming": (60, 200), "ramping": (40, 140), "active": (25, 90)}  # сек между действиями
WARM_DAYS, RAMP_DAYS = 4, 12   # <4д = warming, <12д = ramping, дальше active


def acct_stage(a):
    if a.get("role") == "frozen":
        return "frozen"
    added = a.get("added")
    if not added:
        return "warming"
    days = (time.time() - added) / 86400
    return "warming" if days < WARM_DAYS else ("ramping" if days < RAMP_DAYS else "active")


def actions_today(con, account, action=None):
    since = time.time() - 86400
    if action:
        return con.execute("select count(*) from activity where account=? and action=? and ts>?",
                           (account, action, since)).fetchone()[0]
    return con.execute("select count(*) from activity where account=? and ts>?",
                       (account, since)).fetchone()[0]


def can_do(con, a, action):
    st = acct_stage(a)
    if st == "frozen":
        return False
    caps = CAPS[st]
    if actions_today(con, a["name"]) >= caps["day"]:
        return False
    return actions_today(con, a["name"], action) < caps.get(action, 0)


def record(con, account, action):
    con.execute("insert into activity values(?,?,?)", (account, action, time.time()))
    con.commit()


async def human_delay(a):
    lo, hi = DELAY[acct_stage(a)]
    await asyncio.sleep(random.uniform(lo, hi))


def within_active_hours():
    h = time.gmtime().tm_hour        # UTC; активные ~06-21 UTC (09-24 МСК)
    return 6 <= h <= 21


def mark_role(name, role):
    p = ACCOUNTS
    d = json.loads(p.read_text())
    for x in d:
        if x.get("name") == name:
            x["role"] = role
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2))


def make_client(a):
    proxy = parse_proxy(a.get("proxy"))
    ss = a.get("session_string")
    name = str(a.get("name") or "acc")
    if ss:
        # персистентная файл-сессия -> Telethon кеширует сущности (access_hash),
        # не резолвит юзернеймы повторно -> НЕ флудит ResolveUsername
        from telethon.sessions import SQLiteSession
        sdir = BASE / "sessions"; sdir.mkdir(exist_ok=True)
        fpath = sdir / f"{name}.session"
        if not fpath.exists():
            src = StringSession(ss)
            dst = SQLiteSession(str(fpath))
            dst.set_dc(src.dc_id, src.server_address, src.port)
            dst.auth_key = src.auth_key
            dst.save()
        return TelegramClient(str(sdir / name), API_ID, API_HASH, proxy=proxy)
    s = str(a.get("session") or "")
    if not s:
        raise ValueError(f"account {a.get('name')}: нет session_string и session")
    sess = s[:-8] if s.endswith(".session") else s
    return TelegramClient(sess, API_ID, API_HASH, proxy=proxy)


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.execute("pragma journal_mode=WAL")     # конкурентные читатели+писатель без блокировок
    con.execute("pragma busy_timeout=30000")
    con.executescript("""
    create table if not exists channels(
        id integer primary key, username text, title text,
        participants integer, kind text, via text, ts real);
    create table if not exists messages(
        channel_id integer, msg_id integer, date text, sender_id integer,
        views integer, text text, primary key(channel_id, msg_id));
    create table if not exists members(
        channel_id integer, user_id integer, username text, name text, phone text,
        msgs integer, primary key(channel_id, user_id));
    create table if not exists web_posts(
        channel text, msg_id integer, date text, views text, text text,
        primary key(channel, msg_id));
    create table if not exists activity(account text, action text, ts real);
    create index if not exists ix_activity on activity(account, ts);
    create table if not exists comments(
        channel_id integer, msg_id integer, user_id integer, date text, text text,
        primary key(channel_id, msg_id));
    create index if not exists ix_comments_user on comments(user_id);
    create table if not exists admins(
        channel_id integer, user_id integer, username text, name text, source text);
    create unique index if not exists ix_admins on admins(channel_id, username);
    create table if not exists fwd_sources(
        channel_id integer, src text, cnt integer, primary key(channel_id, src));
    create table if not exists phone_lookup(
        phone text primary key, user_id integer, username text, name text);
    """)
    cols = {r[1] for r in con.execute("pragma table_info(messages)")}
    if "views" not in cols:
        con.execute("alter table messages add column views integer")   # миграция старой БД
    if "msgs" not in {r[1] for r in con.execute("pragma table_info(members)")}:
        con.execute("alter table members add column msgs integer")
    if "reactions" not in {r[1] for r in con.execute("pragma table_info(web_posts)")}:
        con.execute("alter table web_posts add column reactions integer")
    if "has_comments" not in {r[1] for r in con.execute("pragma table_info(channels)")}:
        con.execute("alter table channels add column has_comments integer")  # 1=есть чат, 0=нет
    if "reactions" not in {r[1] for r in con.execute("pragma table_info(comments)")}:
        con.execute("alter table comments add column reactions integer")     # реакции на коммент
    if "chat_username" not in {r[1] for r in con.execute("pragma table_info(channels)")}:
        con.execute("alter table channels add column chat_username text")    # @ чата обсуждения -> ссылка на коммент
    con.commit()
    return con


def save_channel(con, ch, via):
    # insert or IGNORE: не затирать cls/about/gifted/subtopic у уже известных каналов
    con.execute(
        "insert or ignore into channels(id,username,title,participants,kind,via,ts)"
        " values(?,?,?,?,?,?,?)",
        (ch.id, getattr(ch, "username", None), getattr(ch, "title", None),
         getattr(ch, "participants_count", None),
         "group" if getattr(ch, "megagroup", False) else "channel", via, time.time()))


# ── discover: keyword search + recursive similar-channels ─────────────────────

async def cmd_discover(args):
    a = search_account()  # только премиум-аккаунт для поиска
    con = db()
    client = make_client(a)
    async with client:
        me = await client.get_me()
        premium = bool(getattr(me, "premium", False))
        print(f"аккаунт {me.username or me.id} premium={premium}")

        seen = set()
        seeds = []
        for kw in args.keywords or []:
            try:
                r = await client(SearchRequest(q=kw, limit=100))
            except FloodWaitError as e:
                print(f"floodwait {e.seconds}s"); await asyncio.sleep(e.seconds); continue
            for ch in r.chats:
                if isinstance(ch, Channel) and ch.id not in seen:
                    seen.add(ch.id); seeds.append(ch)
                    save_channel(con, ch, f"search:{kw}")
            await asyncio.sleep(1.5)
        con.commit()
        print(f"по ключевым словам найдено каналов: {len(seeds)}")

        # BFS по похожим каналам; premium -> глубже выдача на каждом шаге
        frontier = list(seeds) + [u for u in (args.seed or [])]
        for d in range(args.depth):
            nxt = []
            for ch in frontier:
                try:
                    ent = await client.get_input_entity(ch)
                    rec = await client(GetChannelRecommendationsRequest(channel=ent))
                except (FloodWaitError,) as e:
                    await asyncio.sleep(getattr(e, "seconds", 30)); continue
                except Exception:
                    continue
                for r in rec.chats:
                    if isinstance(r, Channel) and r.id not in seen:
                        seen.add(r.id); nxt.append(r)
                        save_channel(con, r, "recommendation")
                await asyncio.sleep(1.2)
            con.commit()
            print(f"глубина {d+1}: +{len(nxt)} каналов (всего {len(seen)})")
            frontier = nxt
            if not nxt:
                break

    total = con.execute("select count(*) from channels").fetchone()[0]
    print(f"итого каналов в БД: {total}")
    con.close()


# ── parse: fan-out по аккаунтам, история сообщений (+участники) ───────────────

async def _parse_channel(client, con, target, limit, members):
    try:
        ent = await client.get_entity(target)
    except Exception as e:
        return f"{target}: resolve fail {type(e).__name__}"
    ch_id = ent.id
    last = con.execute("select max(msg_id) from messages where channel_id=?", (ch_id,)).fetchone()[0] or 0
    n = 0
    try:
        async for m in client.iter_messages(ent, limit=limit, min_id=last):
            con.execute("insert or replace into messages(channel_id,msg_id,date,sender_id,views,text)"
                        " values(?,?,?,?,?,?)",
                        (ch_id, m.id, m.date.isoformat() if m.date else None,
                         getattr(m, "sender_id", None), getattr(m, "views", None), m.message or ""))
            n += 1
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds)
    mc = 0
    if members:
        try:
            async for u in client.iter_participants(ent, limit=limit):
                con.execute("insert or replace into members values(?,?,?,?,?)",
                            (ch_id, u.id, u.username,
                             " ".join(x for x in (u.first_name, u.last_name) if x), u.phone))
                mc += 1
        except Exception:
            pass
    con.commit()
    return f"{ch_id}: +{n} msg" + (f", +{mc} members" if members else "")


async def _account_worker(a, ch_ids, limit, members):
    con = db()
    client = make_client(a)
    out = []
    async with client:
        for cid in ch_ids:
            out.append(await _parse_channel(client, con, cid, limit, members))
            await asyncio.sleep(2)
    con.close()
    return out


async def cmd_parse(args):
    con = db()
    if args.channels:
        ch_ids = args.channels
    else:  # username-резолв надёжнее bare id (нет access_hash у чужих каналов)
        ch_ids = target_usernames(con, cls=getattr(args, "cls", None))
    con.close()
    if not ch_ids:
        raise SystemExit("нет каналов: сначала discover или передай --channels")
    accs = work_accounts()
    # round-robin каналов по аккаунтам -> параллельно
    buckets = split_buckets(ch_ids, accs)
    res = await asyncio.gather(*(
        _account_worker(a, b, args.limit, args.members)
        for a, b in zip(accs, buckets) if b))
    for line in [x for sub in res for x in sub]:
        print(line)


# ── send: рассылка по задачам с лимитами ──────────────────────────────────────

async def cmd_send(args):
    tasks = json.loads(Path(args.taskfile).read_text(encoding="utf-8"))
    accs = {a["name"]: a for a in load_accounts()}
    by_acc = {}
    for t in tasks:
        by_acc.setdefault(t["account"], []).append(t)

    async def worker(name, items):
        if name not in accs:
            print(f"нет аккаунта {name}"); return
        client = make_client(accs[name])
        async with client:
            for t in items:
                try:
                    await client.send_message(t["to"], t["text"], link_preview=False)
                    print(f"{name} -> {t['to']} ok")
                except FloodWaitError as e:
                    print(f"{name} floodwait {e.seconds}s"); await asyncio.sleep(e.seconds)
                except PeerFloodError:
                    print(f"{name} PeerFlood — стоп аккаунта"); break
                except UserPrivacyRestrictedError:
                    print(f"{name} -> {t['to']} privacy")
                except Exception as e:
                    print(f"{name} -> {t['to']} err {type(e).__name__}")
                await asyncio.sleep(args.pause)  # ponytail: фиксированная пауза; рандомизуй если жёсткие лимиты

    await asyncio.gather(*(worker(n, items) for n, items in by_acc.items()))


# ── stories: массовый просмотр + лайки (мультиаккаунт) ────────────────────────

def _story_ids(res):
    out = res.stories.stories if hasattr(res, "stories") else []
    return [s.id for s in out if type(s).__name__ == "StoryItem"]


async def _stories_worker(a, peers, like, emoji):
    client = make_client(a)
    out = []
    async with client:
        for peer in peers:
            try:
                ent = await client.get_input_entity(peer)
                ids = _story_ids(await client(GetPeerStoriesRequest(peer=ent)))
                if ids:
                    await client(IncrementStoryViewsRequest(peer=ent, id=ids))
                    await client(ReadStoriesRequest(peer=ent, max_id=max(ids)))
                    if like:
                        for sid in ids:
                            await client(SendReactionRequest(
                                peer=ent, story_id=sid, reaction=ReactionEmoji(emoji)))
                            await asyncio.sleep(0.6)
                out.append(f"{a['name']} {peer}: viewed {len(ids)}" + (" +liked" if like and ids else ""))
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as e:
                out.append(f"{a['name']} {peer}: ERR {type(e).__name__}")
            await asyncio.sleep(1)
    return out


async def cmd_stories(args):
    con = db()
    peers = args.peers or target_usernames(con, cls=args.cls, limit=args.limit)
    con.close()
    if not peers:
        raise SystemExit("нет целей: передай --peers или сделай discover")
    accs = work_accounts()  # каждый рабочий аккаунт смотрит все цели -> больше просмотров
    res = await asyncio.gather(*(_stories_worker(a, peers, args.like, args.emoji) for a in accs))
    for line in [x for sub in res for x in sub]:
        print(line)


# ── react: реакции на свежие посты каналов (буст + попасть в поле зрения) ──────

REACT_EMOJI = ["👍", "🔥", "❤", "👏", "🚀", "⚡", "💯"]


async def _react_worker(a, channels, count, pause):
    client = make_client(a)
    out = []
    async with client:
        for name in channels:
            try:
                ent = await client.get_entity(name)
                ids = [m.id async for m in client.iter_messages(ent, limit=count) if m.id]
                done = 0
                for mid in ids:
                    try:
                        await client(MsgSendReaction(peer=ent, msg_id=mid,
                                                     reaction=[ReactionEmoji(random.choice(REACT_EMOJI))]))
                        done += 1
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds)
                    except Exception:
                        pass
                    await asyncio.sleep(pause)
                out.append(f"{a['name']} {name}: реакций {done}")
            except Exception as e:
                out.append(f"{a['name']} {name}: ERR {type(e).__name__}")
            await asyncio.sleep(pause)
    return out


async def cmd_react(args):
    con = db()
    targets = args.channels or target_usernames(con, cls=args.cls, limit=args.limit)
    con.close()
    accs = work_accounts()
    buckets = split_buckets(targets, accs)   # распределяем каналы по аккаунтам
    res = await asyncio.gather(*(_react_worker(a, b, args.count, args.pause)
                                 for a, b in zip(accs, buckets) if b))
    for line in [x for sub in res for x in sub]:
        print(line)


# ── engage: БЕЗОПАСНАЯ вовлечённость через governor (анти-фриз) ───────────────

def _is_frozen(e):
    return type(e).__name__ == "FrozenMethodInvalidError"


async def _engage_account(a, targets, force_hours):
    con = db()
    st = acct_stage(a)
    if st == "frozen":
        con.close(); return f"{a['name']}: заморожен, пропуск"
    if not force_hours and not within_active_hours():
        con.close(); return f"{a['name']}: вне активных часов, пропуск"
    if actions_today(con, a["name"]) >= CAPS[st]["day"]:
        con.close(); return f"{a['name']} [{st}]: дневной лимит уже исчерпан"
    client = make_client(a)
    did = Counter()
    try:
        await asyncio.wait_for(client.connect(), 45)
        random.shuffle(targets)
        for name in targets:
            if actions_today(con, a["name"]) >= CAPS[st]["day"]:
                break
            if not (can_do(con, a, "story_react") or can_do(con, a, "post_react")):
                break
            try:
                ent = await client.get_input_entity(name)
                if can_do(con, a, "story_view"):
                    ids = _story_ids(await client(GetPeerStoriesRequest(peer=ent)))
                    if ids:
                        await client(IncrementStoryViewsRequest(peer=ent, id=ids[:3]))
                        record(con, a["name"], "story_view"); did["story_view"] += 1
                        if can_do(con, a, "story_react"):
                            await client(SendReactionRequest(peer=ent, story_id=ids[0],
                                                             reaction=ReactionEmoji("❤")))
                            record(con, a["name"], "story_react"); did["story_react"] += 1
                        await human_delay(a); continue
                if can_do(con, a, "post_react"):
                    async for m in client.iter_messages(ent, limit=1):
                        if m and m.id:
                            await client(MsgSendReaction(peer=ent, msg_id=m.id,
                                         reaction=[ReactionEmoji(random.choice(REACT_EMOJI))]))
                            record(con, a["name"], "post_react"); did["post_react"] += 1
                            await human_delay(a)
                        break
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as e:
                if _is_frozen(e):
                    mark_role(a["name"], "frozen")
                    con.close()
                    return f"{a['name']}: ЗАМОРОЖЕН -> role=frozen (успел {dict(did)})"
    except Exception as e:
        con.close(); return f"{a['name']}: conn ERR {type(e).__name__}"
    finally:
        try: await client.disconnect()
        except Exception: pass
    con.close()
    return f"{a['name']} [{st}]: {dict(did) or 'нет действий'}"


async def cmd_engage(args):
    con = db(); targets = args.channels or target_usernames(con, cls=args.cls or "it_blogger"); con.close()
    if not targets:
        raise SystemExit("нет целей")
    accs = work_accounts()

    async def staggered(a):
        await asyncio.sleep(random.uniform(0, args.spread))   # разнос стартов аккаунтов
        return await _engage_account(a, list(targets), args.force_hours)

    for r in await asyncio.gather(*(staggered(a) for a in accs)):
        print(r)


# ── warm: мягкий прогрев (онлайн + чтение + просмотр сторис, БЕЗ реакций) ──────

async def _warm_account(a, targets):
    con = db()
    st = acct_stage(a)
    if st == "frozen":
        con.close(); return f"{a['name']}: заморожен"
    client = make_client(a)
    try:
        await asyncio.wait_for(client.connect(), 45)
        await client.get_me()
        dialogs = await client.get_dialogs(limit=15)          # пассивное чтение
        record(con, a["name"], "read")
        seen = 0
        for name in random.sample(targets, min(3, len(targets))):
            if not can_do(con, a, "story_view"):
                break
            try:
                ent = await client.get_input_entity(name)
                ids = _story_ids(await client(GetPeerStoriesRequest(peer=ent)))
                if ids:
                    await client(IncrementStoryViewsRequest(peer=ent, id=ids[:2]))
                    record(con, a["name"], "story_view"); seen += 1
                await human_delay(a)
            except Exception as e:
                if _is_frozen(e):
                    mark_role(a["name"], "frozen"); con.close()
                    return f"{a['name']}: ЗАМОРОЖЕН"
        con.close()
        return f"{a['name']} [{st}]: онлайн, диалогов {len(dialogs)}, сторис-просмотров {seen}"
    except Exception as e:
        con.close(); return f"{a['name']}: ERR {type(e).__name__}"
    finally:
        try: await client.disconnect()
        except Exception: pass


async def cmd_warm(args):
    con = db(); targets = target_usernames(con, cls="it_blogger", limit=100); con.close()
    accs = work_accounts()

    async def staggered(a):
        await asyncio.sleep(random.uniform(0, args.spread))
        return await _warm_account(a, list(targets))

    for r in await asyncio.gather(*(staggered(a) for a in accs)):
        print(r)


# ── gift: подарки звёздами (в канал или юзеру) ────────────────────────────────

async def cmd_gift(args):
    a = load_accounts()[0] if not args.account else next(
        x for x in load_accounts() if x["name"] == args.account)
    client = make_client(a)
    async with client:
        bal = (await client(GetStarsStatusRequest(peer=InputPeerSelf()))).balance
        bal = getattr(bal, "amount", bal)
        gifts = [g for g in (await client(GetStarGiftsRequest(hash=0))).gifts]
        if not args.send:
            print(f"баланс звёзд: {bal}")
            print("доступные подарки (id | звёзд | limited/sold_out):")
            for g in sorted(gifts, key=lambda g: g.stars):
                lim = "limited" if getattr(g, "limited", False) else ""
                print(f"  {g.id} | {g.stars}★ | {lim}{' SOLD_OUT' if getattr(g,'sold_out',False) else ''}")
            cheapest = min(g.stars for g in gifts if not getattr(g, "sold_out", False))
            print(f"самый дешёвый: {cheapest}★")
            return
        msg = TextWithEntities(text=args.message, entities=[]) if args.message else None
        peer = await client.get_input_entity(args.send)
        inv = InputInvoiceStarGift(peer=peer, gift_id=int(args.gift_id), message=msg)
        form = await client(GetPaymentFormRequest(invoice=inv))
        await client(SendStarsFormRequest(form_id=form.form_id, invoice=inv))
        print(f"{a['name']}: подарок {args.gift_id} -> {args.send}"
              + (f" с текстом «{args.message}»" if args.message else "") + f" (баланс был {bal})")


# ── rank: рейтинг каналов ─────────────────────────────────────────────────────

def cmd_rank(args):
    con = db()
    rows = con.execute(
        "select id,username,title,participants from channels where participants is not null").fetchall()
    scored = []
    for cid, u, t, p in rows:
        av = con.execute(
            "select avg(views) from (select views from messages where channel_id=? "
            "and views is not null order by msg_id desc limit ?)", (cid, args.window)).fetchone()[0]
        eng = round(av / p, 3) if (av and p) else None  # ER = ср.просмотры / подписчиков
        scored.append((p, u, t, int(av) if av else None, eng))
    key = (lambda r: (r[4] or -1)) if args.by == "engagement" else (lambda r: r[0] or 0)
    top = sorted(scored, key=key, reverse=True)[:args.top]
    print(f"РЕЙТИНГ каналов по '{args.by}' (top {args.top}):")
    print(f"{'подписч':>8}  {'ср.просм':>8}  {'ER':>6}  канал")
    for p, u, t, av, eng in top:
        print(f"{p or 0:>8}  {av if av is not None else '-':>8}  {eng if eng is not None else '-':>6}  @{u} {(t or '')[:32]}")
    con.close()


# ── web: парсинг публичных каналов без аккаунта (t.me/s/<name>) ────────────────

def _web_page(name, before):
    url = f"https://t.me/s/{name}" + (f"?before={before}" if before else "")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def _num(s):
    s = (s or "").strip().replace(",", ".")
    mult = 1
    if s[-1:].upper() == "K":
        mult, s = 1000, s[:-1]
    elif s[-1:].upper() == "M":
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except Exception:
        return 0


def cmd_web(args):
    con = db()
    targets = args.channels or target_usernames(con, cls=args.cls, limit=args.limit)
    for name in targets:
        name = str(name).lstrip("@")
        before, got = "", 0
        try:
            for _ in range(args.pages):
                page = _web_page(name, before)
                blocks = page.split("tgme_widget_message_wrap")
                ids = []
                for b in blocks:
                    mid = re.search(r'data-post="[^"/]+/(\d+)"', b)
                    if not mid:
                        continue
                    mid = int(mid.group(1)); ids.append(mid)
                    txt = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', b, re.S)
                    txt = html.unescape(re.sub(r"<[^>]+>", "", txt.group(1))).strip() if txt else ""
                    vм = re.search(r'tgme_widget_message_views"[^>]*>([^<]+)<', b)
                    dt = re.search(r'<time[^>]*datetime="([^"]+)"', b)
                    # реакции: сумма всех счётчиков в блоке реакций
                    rcounts = re.findall(r'tgme_reaction[^>]*>.*?</i>\s*([0-9.,]+[KM]?)', b, re.S)
                    reactions = sum(_num(x) for x in rcounts)
                    con.execute("insert or replace into web_posts(channel,msg_id,date,views,reactions,text)"
                                " values(?,?,?,?,?,?)",
                                (name, mid, dt.group(1) if dt else None,
                                 vм.group(1) if vм else None, reactions, txt))
                if not ids:
                    break
                got += len(ids); before = min(ids); con.commit()
            print(f"{name}: +{got} постов (web, без аккаунта)")
        except Exception as e:
            print(f"{name}: web ERR {type(e).__name__}: {str(e)[:80]}")
    con.close()


# ── active: активные участники канала через его чат обсуждений ─────────────────

def _msg_reactions(m):
    r = getattr(m, "reactions", None)
    if not r or not getattr(r, "results", None):
        return 0
    return sum(getattr(x, "count", 0) for x in r.results)


async def _active_worker(a, targets, limit, save_text=False):
    con = db()
    client = make_client(a)
    out = []
    try:
        await asyncio.wait_for(client.connect(), 40)   # дохлый прокси -> не виснем
    except Exception:
        con.close()
        return [f"{a['name']}: connect fail (прокси?), пропущено {len(targets)} каналов"]
    try:
        for name in targets:
            try:
                ch = await asyncio.wait_for(client.get_entity(name), 40)
                full = await asyncio.wait_for(client(GetFullChannelRequest(channel=ch)), 40)
                linked = getattr(full.full_chat, "linked_chat_id", None)
                if not linked:   # нет чата обсуждений -> реальных комментаторов не собрать
                    con.execute("update channels set has_comments=0 where id=?", (ch.id,)); con.commit()
                    out.append(f"{a['name']} {name}: нет чата обсуждений")
                    await asyncio.sleep(2); continue
                con.execute("update channels set has_comments=1 where id=?", (ch.id,))
                ent = await client.get_entity(linked)
                cnt, names = Counter(), {}
                async for m in client.iter_messages(ent, limit=limit):
                    fid = m.from_id
                    if isinstance(fid, PeerUser):   # только реальные люди, не каналы
                        uid = fid.user_id
                        cnt[uid] += 1
                        if uid not in names:
                            s = m.sender or await m.get_sender()
                            names[uid] = (getattr(s, "username", None),
                                          " ".join(x for x in (getattr(s, "first_name", None),
                                                               getattr(s, "last_name", None)) if x))
                        if save_text and m.message:   # сохраняем текст коммента + реакции
                            con.execute("insert or replace into comments(channel_id,msg_id,user_id,date,text,reactions)"
                                        " values(?,?,?,?,?,?)",
                                        (ch.id, m.id, uid, m.date.isoformat() if m.date else None,
                                         m.message, _msg_reactions(m)))
                for uid, c in cnt.items():
                    u, nm = names.get(uid, (None, None))
                    if u and u.lower().endswith("bot"):   # пропускаем ботов-модераторов
                        continue
                    con.execute("insert or replace into members(channel_id,user_id,username,name,phone,msgs)"
                                " values(?,?,?,?,?,?)", (ch.id, uid, u, nm, None, c))
                con.commit()
                out.append(f"{a['name']} {name}: людей {len(cnt)}")
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as e:
                out.append(f"{a['name']} {name}: ERR {type(e).__name__}")
            await asyncio.sleep(2)
    finally:
        try: await client.disconnect()
        except Exception: pass
    con.close()
    return out


async def cmd_active(args):
    con = db()
    targets = args.channels or target_usernames(con, cls=args.cls, limit=args.limit)
    con.close()
    random.shuffle(targets)   # перемешиваем -> повторный прогон покроет пропущенные дохлыми прокси
    accs = work_accounts()
    buckets = split_buckets(targets, accs)
    res = await asyncio.gather(*(_active_worker(a, b, args.msgs, args.save_text)
                                 for a, b in zip(accs, buckets) if b))
    for line in [x for sub in res for x in sub]:
        print(line)


# ── hascomm: быстрый детект «есть ли чат обсуждений» (GetFullChannel) ─────────

async def _hascomm_worker(a, targets):
    con = db()
    client = make_client(a)
    try:
        await asyncio.wait_for(client.connect(), 40)
    except Exception:
        con.close(); return (0, 0, 1)
    n1 = n0 = err = 0
    try:
        for name in targets:
            try:
                ch = await asyncio.wait_for(client.get_entity(name), 40)
                full = await asyncio.wait_for(client(GetFullChannelRequest(channel=ch)), 40)
                has = 1 if getattr(full.full_chat, "linked_chat_id", None) else 0
                con.execute("update channels set has_comments=? where id=?", (has, ch.id))
                n1 += has; n0 += (1 - has)
                if (n1 + n0) % 15 == 0:
                    con.commit()
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception:
                err += 1
            await asyncio.sleep(1)
        con.commit()
    finally:
        try: await client.disconnect()
        except Exception: pass
    con.close()
    return (n1, n0, err)


async def cmd_hascomm(args):
    con = db()
    targets = args.channels or target_usernames(con, cls=args.cls, limit=args.limit)
    con.close()
    random.shuffle(targets)
    accs = work_accounts()
    buckets = split_buckets(targets, accs)
    res = await asyncio.gather(*(_hascomm_worker(a, b) for a, b in zip(accs, buckets) if b))
    n1 = sum(r[0] for r in res); n0 = sum(r[1] for r in res); err = sum(r[2] for r in res)
    print(f"есть чат обсуждений: {n1} | нет: {n0} | ошибок/пропущено: {err}")


# ── profiles: аудит заполнения профилей аккаунтов ─────────────────────────────

async def cmd_profiles(args):
    from telethon.tl.functions.users import GetFullUserRequest
    accs = work_accounts() if not args.all else load_accounts()

    async def one(a):
        client = make_client(a)
        try:
            await asyncio.wait_for(client.connect(), 45)
            me = await client.get_me()
            full = await client(GetFullUserRequest(me))
            bio = getattr(full.full_user, "about", None)
            return (a["name"], f"@{me.username}" if me.username else "—", me.first_name or "—",
                    me.last_name or "", "фото:да" if me.photo else "фото:НЕТ",
                    "био:" + (bio[:30] if bio else "НЕТ"), f"prem:{bool(getattr(me,'premium',False))}")
        except Exception as e:
            return (a["name"], f"ERR {type(e).__name__}")
        finally:
            try: await client.disconnect()
            except Exception: pass

    for r in await asyncio.gather(*(one(a) for a in accs)):
        print("  " + " | ".join(str(x) for x in r))


# ── monitor: инкрементальный сбор СВЕЖИХ комментов (min_id) ──────────────────

async def _monitor_worker(a, targets):
    con = db()
    client = make_client(a)
    out = []
    try:
        await asyncio.wait_for(client.connect(), 40)
    except Exception:
        con.close(); return [f"{a['name']}: connect fail"]
    try:
        for name in targets:
            try:
                ch = await asyncio.wait_for(client.get_entity(name), 40)
                full = await asyncio.wait_for(client(GetFullChannelRequest(channel=ch)), 40)
                linked = getattr(full.full_chat, "linked_chat_id", None)
                if not linked:
                    continue
                chat = await client.get_entity(linked)
                # @ чата обсуждения: даёт прямую ссылку на коммент t.me/<chat>/<msg_id>
                cu = getattr(chat, "username", None)
                if cu:
                    con.execute("update channels set chat_username=? where id=?", (cu, ch.id))
                last = con.execute("select max(msg_id) from comments where channel_id=?", (ch.id,)).fetchone()[0] or 0
                new = 0
                async for m in client.iter_messages(chat, min_id=last, limit=200):   # только НОВЫЕ
                    fid = m.from_id
                    if isinstance(fid, PeerUser) and m.message:
                        s = m.sender                       # уже в ответе iter_messages, без доп. запроса
                        un = getattr(s, "username", None) if s else None
                        nm = (" ".join(x for x in (getattr(s, "first_name", None),
                                                   getattr(s, "last_name", None)) if x)) if s else None
                        con.execute("insert or replace into comments(channel_id,msg_id,user_id,date,text,reactions)"
                                    " values(?,?,?,?,?,?)",
                                    (ch.id, m.id, fid.user_id, m.date.isoformat() if m.date else None,
                                     m.message, _msg_reactions(m)))
                        con.execute("insert into members(channel_id,user_id,username,name,phone,msgs)"
                                    " values(?,?,?,?,?,1) on conflict(channel_id,user_id) do update set "
                                    "msgs=msgs+1, username=coalesce(excluded.username,members.username), "
                                    "name=coalesce(excluded.name,members.name)",
                                    (ch.id, fid.user_id, un, nm or None, None))
                        new += 1
                con.commit()
                if new:
                    out.append(f"{a['name']} {name}: +{new} свежих")
            except FloodWaitError as e:
                if e.seconds > 600:                    # большой флуд -> не спим часами, выходим
                    out.append(f"{a['name']}: FloodWait {e.seconds}s, стоп")
                    break
                await asyncio.sleep(e.seconds)
            except Exception as e:
                out.append(f"{a['name']} {name}: ERR {type(e).__name__}")
            await asyncio.sleep(2)
    finally:
        try: await client.disconnect()
        except Exception: pass
    con.close()
    return out


async def cmd_monitor(args):
    con = db(); targets = args.channels or target_usernames(con, cls=args.cls or "it_blogger", limit=args.limit); con.close()
    targets = sorted(targets)   # СТАБИЛЬНО: аккаунт всегда берёт свои каналы -> кеш access_hash работает
    accs = work_accounts()
    buckets = split_buckets(targets, accs)
    res = await asyncio.gather(*(_monitor_worker(a, b) for a, b in zip(accs, buckets) if b))
    lines = [x for sub in res for x in sub]
    print("\n".join(lines) if lines else "новых комментов нет")


# ── admins: владельцы/админы + контакты из описания (Maltego-стиль) ──────────

HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{4,32})")


async def _admins_worker(a, targets):
    con = db()
    client = make_client(a)
    out = []
    async with client:
        for name in targets:
            try:
                ent = await client.get_entity(name)
                found = 0
                # 1) контакты из описания канала (по рекламе @X / связь @Y)
                r = con.execute("select about from channels where username=?", (name,)).fetchone()
                for h in set(HANDLE_RE.findall((r[0] if r else "") or "")):
                    if h.lower() == name.lower():
                        continue
                    con.execute("insert or ignore into admins(channel_id,user_id,username,name,source)"
                                " values(?,?,?,?,?)", (ent.id, None, h, None, "description"))
                    found += 1
                # 2) явные админы (часто скрыты у broadcast -> ловим исключение)
                try:
                    p = await client(GetParticipantsRequest(ent, ChannelParticipantsAdmins(), 0, 100, hash=0))
                    for u in p.users:
                        uname = u.username or ("#" + str(u.id))
                        nm = " ".join(x for x in (u.first_name, u.last_name) if x)
                        con.execute("insert or ignore into admins(channel_id,user_id,username,name,source)"
                                    " values(?,?,?,?,?)", (ent.id, u.id, uname, nm, "admin"))
                        found += 1
                except Exception:
                    pass
                con.commit()
                out.append(f"{name}: контактов/админов +{found}")
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as e:
                out.append(f"{name}: ERR {type(e).__name__}")
            await asyncio.sleep(2)
    con.close()
    return out


async def cmd_admins(args):
    con = db(); targets = args.channels or target_usernames(con, cls=args.cls or "it_blogger", limit=args.limit); con.close()
    accs = work_accounts()
    buckets = split_buckets(targets, accs)
    for line in [x for sub in await asyncio.gather(
            *(_admins_worker(a, b) for a, b in zip(accs, buckets) if b)) for x in sub]:
        print(line)


# ── forwards: первоисточники репостов -> расширение базы каналов ──────────────

async def _forwards_worker(a, targets, limit):
    con = db()
    client = make_client(a)
    out = []
    async with client:
        for name in targets:
            try:
                ent = await client.get_entity(name)
                srcs = Counter()
                async for m in client.iter_messages(ent, limit=limit):
                    f = getattr(m, "fwd_from", None)
                    if f and getattr(f, "from_id", None):
                        try:
                            src = await client.get_entity(f.from_id)
                            u = getattr(src, "username", None)
                            if u:
                                srcs[u] += 1
                        except Exception:
                            pass
                for u, cnt in srcs.items():
                    con.execute("insert or replace into fwd_sources(channel_id,src,cnt) values(?,?,?)",
                                (ent.id, u, cnt))
                con.commit()
                out.append(f"{name}: источников {len(srcs)}")
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as e:
                out.append(f"{name}: ERR {type(e).__name__}")
            await asyncio.sleep(2)
    con.close()
    return out


async def cmd_forwards(args):
    con = db(); targets = args.channels or target_usernames(con, cls=args.cls or "it_blogger", limit=args.limit); con.close()
    accs = work_accounts()
    buckets = split_buckets(targets, accs)
    for line in [x for sub in await asyncio.gather(
            *(_forwards_worker(a, b, args.msgs) for a, b in zip(accs, buckets) if b)) for x in sub]:
        print(line)


# ── resolve: телефон -> Telegram-профиль (осторожно, импорт контактов флагится)

async def cmd_resolve(args):
    phones = args.phones or [x.strip() for x in open(args.file) if x.strip()]
    a = work_accounts()[0]
    client = make_client(a)
    con = db()
    async with client:
        for ph in phones:
            try:
                res = await client(ImportContactsRequest(
                    [InputPhoneContact(client_id=0, phone=ph, first_name="x", last_name="")]))
                if res.users:
                    u = res.users[0]
                    nm = " ".join(x for x in (u.first_name, u.last_name) if x)
                    con.execute("insert or replace into phone_lookup values(?,?,?,?)",
                                (ph, u.id, u.username, nm)); con.commit()
                    print(f"{ph} -> id={u.id} @{u.username} {nm}")
                    await client(DeleteContactsRequest(id=[u.id]))   # не копим контакты
                else:
                    print(f"{ph} -> не найден")
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as e:
                print(f"{ph} -> ERR {type(e).__name__}")
            await asyncio.sleep(random.uniform(5, 12))   # мягко, чтобы не флагить
    con.close()


# ── stats: стадии аккаунтов + израсходованные дневные лимиты ─────────────────

def cmd_stats(args):
    con = db()
    accs = load_accounts()
    print(f"{'аккаунт':<20} {'роль':<8} {'стадия':<8} {'сегодня':<8} лимит/день")
    for a in accs:
        role = a.get("role", "work")
        st = acct_stage(a)
        used = actions_today(con, a["name"])
        cap = CAPS.get(st, {}).get("day", "-") if st != "frozen" else "-"
        print(f"{a['name']:<20} {role:<8} {st:<8} {used:<8} {cap}")
    con.close()


# ── validate ──────────────────────────────────────────────────────────────────

async def cmd_validate(args):
    accs = load_accounts()
    sem = asyncio.Semaphore(6)

    async def chk(a):
        async with sem:
            client = make_client(a)
            try:
                await asyncio.wait_for(client.connect(), 45)
                if not await client.is_user_authorized():
                    return (a.get("name"), "unauthorized")
                me = await client.get_me()
                return (a.get("name"), f"OK id={me.id} @{me.username} premium={bool(getattr(me,'premium',False))}")
            except Exception as e:
                return (a.get("name"), f"ERR {type(e).__name__}: {str(e)[:80]}")
            finally:
                try: await client.disconnect()
                except Exception: pass

    for name, st in await asyncio.gather(*(chk(a) for a in accs)):
        print(f"{name}: {st}")


# ── selfcheck (offline) ───────────────────────────────────────────────────────

def cmd_selfcheck(args):
    assert parse_proxy("u:p@h.com:10000") == (socks.SOCKS5, "h.com", 10000, True, "u", "p")
    assert parse_proxy("socks5://a:b@h:9") == (socks.SOCKS5, "h", 9, True, "a", "b")
    assert parse_proxy("") is None and parse_proxy("noatsign") is None
    global DB
    DB = BASE / "_selfcheck.db"
    if DB.exists(): DB.unlink()
    con = db()
    con.execute("insert or replace into messages values(?,?,?,?,?,?)", (1, 5, "d", 9, 100, "hi"))
    con.execute("insert or replace into messages values(?,?,?,?,?,?)", (1, 5, "d", 9, 200, "hi"))  # dedup
    con.execute("insert or replace into channels(id,username,title,participants,kind,via,ts)"
                " values(?,?,?,?,?,?,?)", (1, "c", "T", 1000, "channel", "x", 0))
    con.commit()
    assert con.execute("select count(*) from messages").fetchone()[0] == 1
    assert con.execute("select max(msg_id) from messages where channel_id=1").fetchone()[0] == 5
    assert con.execute("select views from messages where channel_id=1").fetchone()[0] == 200
    con.close(); DB.unlink()
    print("selfcheck OK")


def main():
    p = argparse.ArgumentParser(prog="tgauto")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selfcheck")
    sub.add_parser("validate")
    d = sub.add_parser("discover")
    d.add_argument("-k", "--keywords", nargs="*", default=[])
    d.add_argument("--seed", nargs="*", default=[], help="@username сид-каналов")
    d.add_argument("--depth", type=int, default=2)
    pa = sub.add_parser("parse")
    pa.add_argument("--channels", nargs="*", help="id/@username; по умолч. все из БД")
    pa.add_argument("--limit", type=int, default=1000)
    pa.add_argument("--members", action="store_true")
    pa.add_argument("--cls", help="фильтр по классу: it_blogger/it_media/it_jobs")
    s = sub.add_parser("send")
    s.add_argument("taskfile")
    s.add_argument("--pause", type=int, default=60)
    st = sub.add_parser("stories")
    st.add_argument("--peers", nargs="*", help="@username; по умолч. топ из БД")
    st.add_argument("--limit", type=int, default=50)
    st.add_argument("--like", action="store_true")
    st.add_argument("--emoji", default="❤")
    st.add_argument("--cls", help="фильтр по классу: it_blogger/it_media/it_jobs")
    rc = sub.add_parser("react")
    rc.add_argument("--channels", nargs="*")
    rc.add_argument("--limit", type=int, default=300, help="сколько каналов из БД")
    rc.add_argument("--count", type=int, default=3, help="сколько последних постов реагировать")
    rc.add_argument("--pause", type=int, default=2)
    rc.add_argument("--cls", help="фильтр по классу: it_blogger/it_media/it_jobs")
    en = sub.add_parser("engage")   # БЕЗОПАСНО: реакции через governor (дневные лимиты+паузы)
    en.add_argument("--channels", nargs="*")
    en.add_argument("--cls", help="класс целей (по умолч. it_blogger)")
    en.add_argument("--spread", type=int, default=60, help="разброс старта аккаунтов, сек")
    en.add_argument("--force-hours", action="store_true", help="игнорировать активные часы")
    wm = sub.add_parser("warm")     # мягкий прогрев новых аккаунтов
    wm.add_argument("--spread", type=int, default=60)
    sub.add_parser("stats")         # стадии + израсходованные лимиты
    g = sub.add_parser("gift")
    g.add_argument("--account")
    g.add_argument("--send", help="@peer получателя; без него — просто список подарков и баланс")
    g.add_argument("--gift-id", dest="gift_id")
    g.add_argument("--message", help="текст к подарку (напр. упоминание твоего канала)")
    r = sub.add_parser("rank")
    r.add_argument("--by", choices=["subs", "engagement"], default="subs")
    r.add_argument("--top", type=int, default=30)
    r.add_argument("--window", type=int, default=20, help="сколько последних постов для ER")
    w = sub.add_parser("web")   # без аккаунта, t.me/s/<name>
    w.add_argument("--channels", nargs="*", help="@username; по умолч. топ из БД")
    w.add_argument("--pages", type=int, default=3)
    w.add_argument("--limit", type=int, default=20)
    w.add_argument("--cls", help="фильтр по классу: it_blogger/it_media/it_jobs")
    ac = sub.add_parser("active")
    ac.add_argument("--channels", nargs="*")
    ac.add_argument("--limit", type=int, default=20, help="сколько каналов из БД")
    ac.add_argument("--msgs", type=int, default=300, help="сколько сообщений чата сканировать")
    ac.add_argument("--cls", help="фильтр по классу: it_blogger/it_media/it_jobs")
    ac.add_argument("--save-text", action="store_true", help="сохранять текст комментов в comments")
    pr = sub.add_parser("profiles")
    pr.add_argument("--all", action="store_true", help="включая премиум-поисковик")
    am = sub.add_parser("admins")   # владельцы/контакты каналов (Maltego)
    am.add_argument("--channels", nargs="*")
    am.add_argument("--cls", help="класс (по умолч. it_blogger)")
    am.add_argument("--limit", type=int, default=1000)
    fw = sub.add_parser("forwards") # первоисточники репостов
    fw.add_argument("--channels", nargs="*")
    fw.add_argument("--cls", help="класс (по умолч. it_blogger)")
    fw.add_argument("--limit", type=int, default=1000)
    fw.add_argument("--msgs", type=int, default=200)
    rs = sub.add_parser("resolve")  # телефон -> профиль
    rs.add_argument("--phones", nargs="*")
    rs.add_argument("--file", help="файл со списком телефонов")
    mo = sub.add_parser("monitor")  # свежие комменты (инкрементально)
    mo.add_argument("--channels", nargs="*")
    mo.add_argument("--cls", help="класс (по умолч. it_blogger)")
    mo.add_argument("--limit", type=int, default=1000)
    hc = sub.add_parser("hascomm")  # детект: есть ли чат обсуждений
    hc.add_argument("--channels", nargs="*")
    hc.add_argument("--cls")
    hc.add_argument("--limit", type=int, default=2000)
    args = p.parse_args()

    if args.cmd == "selfcheck":
        return cmd_selfcheck(args)
    if args.cmd == "rank":
        return cmd_rank(args)
    if args.cmd == "web":
        return cmd_web(args)
    if args.cmd == "stats":
        return cmd_stats(args)
    fn = {"validate": cmd_validate, "discover": cmd_discover, "parse": cmd_parse,
          "send": cmd_send, "stories": cmd_stories, "gift": cmd_gift,
          "active": cmd_active, "profiles": cmd_profiles, "react": cmd_react,
          "engage": cmd_engage, "warm": cmd_warm, "admins": cmd_admins,
          "forwards": cmd_forwards, "resolve": cmd_resolve, "monitor": cmd_monitor,
          "hascomm": cmd_hascomm}[args.cmd]
    asyncio.run(fn(args))


if __name__ == "__main__":
    main()
