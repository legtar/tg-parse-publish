# tgauto — мультиаккаунт Telegram-платформа (что умеем)

Всё в `/opt/telegram-automation/`. Запуск: `venv/bin/python tgauto.py <команда>` (venv = `/root/telegram-voice-bot/venv`).
Данные: `data.db` (SQLite, WAL). Аккаунты: `accounts.json`, основной отдельно `main_account.json`.

## Установка
```
pip install -r requirements.txt
cp config.example.json config.json    # впиши свой bot_token, chat_id, feed_channel, путь к LLM
# accounts.json / main_account.json / config.json / *.session — НЕ коммитить (уже в .gitignore)
```
Все секреты (токен бота, session-строки, ключи LLM, прокси) держатся в локальных JSON, которых нет в репо. См. `config.example.json`.

## Пул аккаунтов и роли
`{name, proxy, session_string, role, added}`. Прокси — DataImpulse SOCKS5, свой sticky-порт у каждого. Роли:
- **search** — премиум-аккаунт, только дискавери (premium даёт глубже похожие каналы).
- **work** — рабочие (парсинг/вовлечённость), стадия по возрасту.
- **gift** — держат звёзды, шлют подарки (отдельный основной аккаунт).
- **frozen** — заморожены, исключены (авто при FrozenMethodInvalidError).

## Анти-фриз (governor)
Каждое действие → таблица `activity`; дневной лимит на аккаунт по СТАДИИ (растёт с возрастом):
| Стадия | Возраст | story_view | story_react | post_react | всего/день |
|--------|---------|-----------|------------|-----------|-----------|
| warming | <4 дней | 15 | 0 | 0 | 18 |
| ramping | 4–12 дней | 40 | 10 | 20 | 55 |
| active | >12 дней | 80 | 25 | 45 | 120 |
Человеческие паузы (warming 60–200с), активные часы 06–21 UTC, разнос старта, авто-frozen.
Свежие аккаунты — только просмотры/чтение, реакции с ramping. НИКОГДА не лить залпом.

## Команды

### Без аккаунтов (риск 0)
- `discover -k <слова> --depth N` — поиск каналов + похожие (premium даёт глубже).
- `web --cls it_blogger` — посты публичных каналов через t.me/s (→ web_posts, для engagement).
- `rank --by subs|engagement` — рейтинг каналов.
- `stats` — стадии аккаунтов + израсходованные лимиты.

### Парсинг аккаунтами (read-only, мягко)
- `parse --cls X [--members]` — история сообщений/участники.
- `active --cls X [--msgs N] [--save-text]` — активные комментаторы из чатов обсуждений
  (реальные люди, PeerUser, без ботов/self). `--save-text` → пишет текст комментов в `comments`.

### OSINT (в стиле Maltego)
- `admins --cls X` — владельцы/рекламные контакты каналов: @контакты из описания + явные админы
  (GetParticipants). → таблица `admins`. База для прямого аутрича/взаимопиара.
- `forwards --cls X --msgs N` — первоисточники репостов (fwd_from) → `fwd_sources` (расширение базы).
- `resolve --phones <...> | --file f` — телефон → Telegram-профиль (ImportContacts, мягко: импорт
  контактов флагится, поэтому паузы 5–12с и удаление контакта после). → `phone_lookup`.
- (пропущено: де-анон через ID стикер-паков — ненадёжно/серо, при нужде добавим отдельно.)

### Вовлечённость — только через governor
- `warm` — прогрев: онлайн + чтение диалогов + просмотр сторис (без реакций).
- `engage --cls X` — безопасные реакции на сторис/посты с дневными лимитами и паузами.
- `stories --cls X --like` / `react --cls X` — прямые (осторожно, лучше `engage`).

### Реклама / рассылка
- `gift --account <name> [--send @peer --gift-id <id> --message "..."]` — подарки звёздами.
- `gift_campaign.py --limit N --pause S` — кампания подарков по мелким IT-блогам (промо-текст, hide_name=False).
- `send <task.json>` — сообщения в каналы/личку (паузы, флуд/приватность).

### Служебное
- `validate`, `profiles`, `selfcheck`.

## Данные (таблицы data.db)
- `channels` — id, username, title, participants, cls (it_blogger/it_media/it_jobs/not_it),
  subtopic (backend/frontend/ml_ai/...), about, gifted, via.
- `web_posts` — посты без аккаунтов (текст, просмотры).
- `members` — активные люди: (канал, человек, кол-во сообщений) — метаданные «где найден».
- `comments` — текст комментов активных людей.
- `admins` — владельцы/контакты каналов (source: description/admin).
- `fwd_sources` — первоисточники репостов.
- `phone_lookup` — телефон → профиль.
- `activity` — журнал действий аккаунтов (для лимитов governor).

## Экспорты (CSV, локально у пользователя)
- `it_channels.csv` — каналы: класс, подтема, подписчики, посты, engagement%, статус подарка, описание.
- `it_active_people.csv` — люди: юзернейм, имя, в скольких каналах, список каналов, всего сообщений, пример коммента.

## Расписание (cron, UTC)
```
30 9,18 * * *  warm                         # прогрев 2x/день
0 8,13,19 * * * engage                       # мягкие реакции 3x/день (активные часы)
0 */2 * * *    gift_campaign --limit 16 --pause 150   # подарки ~190/день вразброс
```

## Текущие цифры (2026-08-09)
Каналов 2815 (985 it_blogger + 378 media + 255 jobs, 1132 not_it). web-постов ~29.6k по 828 каналам.
Активных людей ~7.8k (507 в 2+ каналах). Владельцев/контактов 1010. Подарков отправлено 211.
Пул: 30 аккаунтов (1 search + 22 work-warming + 7 frozen).
