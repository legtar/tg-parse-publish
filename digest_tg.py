# -*- coding: utf-8 -*-
"""Тировый дайджест: каналы делятся по подписчикам, в каждом тире свой топ постов
(нормировка на размер канала -> честное сравнение). + LLM-темы комментов.
  digest_tg.py [comments_N] [hours]   # по умолч. 300, 24ч"""
import sys, os, re, html, json, sqlite3, urllib.request, urllib.parse
from datetime import datetime, timezone

# секреты и адресаты — в config.json рядом со скриптом (см. config.example.json, в .gitignore)
CFG = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")))
DB = CFG.get("db", "data.db")
# эндпоинт выбираем ПО ИМЕНИ модели: llm_endpoints.json общий для всех проектов сервера,
# порядок в нём меняется -> по индексу [0] можно молча уехать на чужую модель
_EPS = json.load(open(CFG["llm_endpoints"]))
_WANT = CFG.get("llm_model", "deepseek-v4-pro")
EP = next((e for e in _EPS if e.get("model") == _WANT), _EPS[0])
LLM_URL = EP["base"].rstrip("/") + "/chat/completions"
KEY = EP.get("key") or EP.get("api_key")
MODEL = EP["model"]
BOT = CFG["bot_token"]
CHAT_ID = CFG["chat_id"]
FEED_CHANNEL = CFG["feed_channel"]      # канал-фид (бот — админ, право постинга)
TARGETS = [CHAT_ID, FEED_CHANNEL]       # шлём и в личку, и в канал
N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
HOURS = int(sys.argv[2]) if len(sys.argv) > 2 else 24
TOP_PER_TIER = 10
# (min, max, label, окно_часов) — окно под частоту постинга: мелкие постят реже -> шире
TIERS = [(100, 1000, "🟢 Микро · 100–1k · 5д", 120), (1000, 5000, "🔵 Малые · 1k–5k · 4д", 96),
         (5000, 20000, "🟣 Средние · 5k–20k · 2д", 48), (20000, 100000, "🟠 Крупные · 20k–100k · 1д", 24),
         (100000, 10**12, "🔴 Топ · 100k+ · 1д", 24)]
CLASSES = [("it_blogger", "👤 IT-БЛОГЕРЫ"), ("it_media", "📰 IT-МЕДИА")]


def pv(s):
    s = (s or "").strip().replace(",", ".")
    m = 1
    if s[-1:].upper() == "K": m, s = 1000, s[:-1]
    elif s[-1:].upper() == "M": m, s = 1_000_000, s[:-1]
    try: return int(float(s) * m)
    except Exception: return 0


def tier_of(subs):
    for lo, hi, lbl, win in TIERS:
        if subs and lo <= subs < hi:
            return lbl, win
    return None


def tiered_posts():
    c = sqlite3.connect(DB, timeout=60)
    now = datetime.now(timezone.utc)
    meta = {u: (s, cl, lang) for u, s, cl, lang in c.execute(
        "select username, participants, cls, lang from channels where username is not null")}
    # каналы с комментами (members + has_comments=1). Для микро-тира фильтр не применяем.
    comment_ch = set(r[0] for r in c.execute(
        "select ch.username from members m join channels ch on ch.id=m.channel_id "
        "where ch.username is not null"))
    comment_ch |= set(r[0] for r in c.execute(
        "select username from channels where has_comments=1 and username is not null"))
    rows = c.execute("select channel, msg_id, date, views, reactions, text from web_posts "
                     "where date is not null and views is not null").fetchall()
    # {класс: {тир: [посты]}}
    buckets = {cl: {lbl: [] for _, _, lbl, _ in TIERS} for cl, _ in CLASSES}
    for ch, mid, date, views, reactions, text in rows:
        s, cl, lang = meta.get(ch, (None, None, None))
        if cl not in buckets:
            continue
        if lang in ("en", "uz", "other"):   # только русскоязычные каналы
            continue
        t = tier_of(s)
        if not t:
            continue
        tl, win = t                                   # win = окно тира в часах
        # фильтр комментов: для микро (100-1k) НЕ требуем, для остальных тиров — обязателен
        is_micro = s is not None and 100 <= s < 1000
        if not is_micro and ch not in comment_ch:
            continue
        try:
            age_h = (now - datetime.fromisoformat(date)).total_seconds() / 3600
        except Exception:
            continue
        if age_h < 0 or age_h > win:                  # per-tier окно
            continue
        v, r = pv(views), (reactions or 0)
        score = (v + 15 * r) / max(s, 100) / (age_h + 2)   # вовлечённость/подписчика/час
        buckets[cl][tl].append((score, ch, mid, age_h, v, r, s, (text or "").replace("\n", " ")[:80]))
    for cl in buckets:
        for lbl in buckets[cl]:
            buckets[cl][lbl].sort(reverse=True)
            seen, dedup = set(), []                       # не более 1 поста с канала
            for it in buckets[cl][lbl]:
                if it[1] in seen:
                    continue
                seen.add(it[1]); dedup.append(it)
            buckets[cl][lbl] = dedup[:TOP_PER_TIER]
    return buckets


# словарь тем/инструментов -> считаем в скольких комментах упоминается (квантованно)
LEX = {
    "Python": r"\bpython\b|питон|пайтон", "JavaScript": r"\bjs\b|javascript|джаваскрипт",
    "TypeScript": r"typescript|\bts\b", "React": r"\breact\b|реакт",
    "Java": r"\bjava\b(?!script)|джава", "Go": r"\bgolang\b|\bgo\b|голанг", "Rust": r"\brust\b|раст",
    "C/C++": r"c\+\+|си плюс|\bc#\b", "SQL/БД": r"\bsql\b|postgres|базы данных|бд\b",
    "Docker/K8s": r"docker|докер|kubernetes|k8s|кубер", "Linux": r"\blinux\b|линукс|убунту",
    "ИИ/LLM": r"chatgpt|\bgpt\b|\bllm\b|нейросет|нейронк|\bии\b|\bai\b|\bml\b|машинное обуч|агент",
    "DevOps": r"devops|девопс|ci/cd|cicd|деплой", "Собеседования": r"собес|интервью|interview|тех.?скрин",
    "Вакансии/найм": r"ваканси|нанима|hiring|резюме|оффер|устро", "Зарплата": r"зарплат|\bзп\b|salary|вилк",
    "Фронтенд": r"фронт|frontend|верстк|\bcss\b|\bhtml\b", "Бэкенд": r"бэкенд|backend|бекенд|\bapi\b",
    "Мобилка": r"android|\bios\b|flutter|мобиль|kotlin|swift", "QA/тесты": r"\bqa\b|тестировщ|автотест|тестиров",
    "Крипта/Web3": r"крипт|blockchain|web3|биткоин|\bton\b", "Обучение/курсы": r"курс|обучен|ментор|стажир|джун",
    "Выгорание": r"выгор|устал|бесит|токсич|депресс",
}


def quant_themes(limit=1000):
    c = sqlite3.connect(DB, timeout=60)
    texts = [r[0].lower() for r in c.execute(
        "select text from comments where text!='' order by date desc limit ?", (limit,)).fetchall()]
    counts = []
    for name, pat in LEX.items():
        rx = re.compile(pat, re.I)
        n = sum(1 for t in texts if rx.search(t))
        if n:
            counts.append((name, n))
    counts.sort(key=lambda x: -x[1])
    return counts[:14], len(texts)


def llm_themes(limit=600):
    c = sqlite3.connect(DB, timeout=60)
    texts = [r[0] for r in c.execute(
        "select text from comments where text!='' order by date desc limit ?", (limit,)).fetchall()]
    if not texts:
        return ""
    sample = "\n".join("- " + t.replace("\n", " ")[:180] for t in texts)
    prompt = ("Свежие комментарии из IT-каналов. Кратко на русском в 4-5 пунктах: главные темы обсуждений, "
              "частые вопросы/боли, общее настроение. По делу, без воды.\n\nКомментарии:\n" + sample)
    body = json.dumps({"model": MODEL, "temperature": 0.3,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(LLM_URL, data=body, method="POST",
                                headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    return json.load(urllib.request.urlopen(req, timeout=180))["choices"][0]["message"]["content"]


def hot_comments(limit=6):
    """Топ комментов по реакциям. Ссылка t.me/<чат>/<msg_id> — только если чат обсуждения публичный."""
    c = sqlite3.connect(DB, timeout=60)
    try:
        rows = c.execute(
            "select co.text, co.reactions, ch.chat_username, co.msg_id, ch.username "
            "from comments co join channels ch on ch.id = co.channel_id "
            "where co.reactions>0 and co.text!='' order by co.reactions desc limit ?",
            (limit,)).fetchall()
    except sqlite3.OperationalError:      # старая БД без chat_username
        return [(t, r, None) for t, r in c.execute(
            "select text, reactions from comments where reactions>0 and text!='' "
            "order by reactions desc limit ?", (limit,)).fetchall()]
    out = []
    for text, react, chat_un, mid, chan_un in rows:
        link = f"https://t.me/{chat_un}/{mid}" if chat_un else (f"https://t.me/{chan_un}" if chan_un else None)
        out.append((text, react, link))
    return out


def md_to_html(t):
    t = html.escape(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", t)
    return t


def send(text):
    # режем по границам секций (пустая строка), чтобы пост не обрывался посреди списка
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        block = para + "\n\n"
        if len(block) > 3800:                       # секция сама больше лимита -> по строкам
            for line in para.split("\n"):
                if cur and len(cur) + len(line) + 1 > 3800:
                    chunks.append(cur.rstrip()); cur = ""
                cur += line + "\n"
            cur += "\n"
        else:
            if cur and len(cur) + len(block) > 3800:
                chunks.append(cur.rstrip()); cur = ""
            cur += block
    if cur.strip():
        chunks.append(cur.rstrip())
    for tgt in TARGETS:
        for ch in chunks:
            data = urllib.parse.urlencode({"chat_id": tgt, "text": ch, "parse_mode": "HTML",
                                           "disable_web_page_preview": "true"}).encode()
            r = json.load(urllib.request.urlopen(
                urllib.request.Request(f"https://api.telegram.org/bot{BOT}/sendMessage", data=data), timeout=30))
            print(f"[{tgt}] chunk ok:", r.get("ok"), "" if r.get("ok") else r.get("description"))


def kfmt(n):
    return f"{n/1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def build_and_send():
    # self-lock через abstract-socket (ядро, кросс-процессно, авто-освобождение при выходе):
    # защита от двойного запуска — крон задублирован в user-crontab и /etc/cron.d (fcntl.flock тут ненадёжен)
    import socket
    global _LOCK_SOCK
    _LOCK_SOCK = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        _LOCK_SOCK.bind("\0telegram_digest_lock")
    except OSError:
        print("digest уже выполняется — выходим (двойной запуск)")
        return
    data = tiered_posts()
    msg = ("📊 <b>Топ постов</b>\n"
           "<i>по классу и размеру канала, нормировано на подписчиков. Окно у тира своё — "
           "мелкие постят реже, им шире.</i>\n\n")
    for cl, cl_lbl in CLASSES:
        seg = data[cl]
        if not any(seg.values()):
            continue
        msg += f"━━━ <b>{cl_lbl}</b> ━━━\n"
        for lo, hi, lbl, win in TIERS:
            posts = seg[lbl]
            if not posts:
                continue
            msg += f"<b>{lbl}</b>\n"
            for i, (score, ch, mid, age, v, r, s, prev) in enumerate(posts, 1):
                er = round(100 * v / s, 1) if s else 0
                msg += (f"{i}. {kfmt(v)}👁 {r}❤ · {er}% охват · {age:.0f}ч\n"
                        f"https://t.me/{ch}/{mid}\n")
            msg += "\n"
    quant, ncomm = quant_themes()
    if quant:
        msg += f"📈 <b>Что упоминают</b> (по {ncomm} свежим комментам)\n"
        msg += " · ".join(f"{name} <b>{n}</b>" for name, n in quant) + "\n\n"
    hot = hot_comments()
    if hot:
        msg += "🔥 <b>Горячие комменты</b>\n"
        for text, rx, link in hot:
            body = html.escape(text.replace(chr(10), " ")[:130])
            msg += (f"❤{rx} — <a href=\"{link}\">{body}</a>\n" if link
                    else f"❤{rx} — <i>{body}</i>\n")
        msg += "\n"
    themes = llm_themes()
    if themes:
        msg += "💬 <b>О чём пишут</b>\n\n" + md_to_html(themes)
    send(msg)
    print("тировый дайджест отправлен")


if __name__ == "__main__":
    build_and_send()
