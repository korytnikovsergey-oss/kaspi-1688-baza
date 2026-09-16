# -*- coding: utf-8 -*-
"""
Сборщик базы 1688 — заходит в каждую карточку товара, как человек.

Что делает, по кругу, БЕСКОНЕЧНО (пока окно открыто):
  1. Открывает обычный поиск 1688 по китайскому запросу (без аккаунта, без входа).
  2. Листает страницы выдачи, как человек: прокручивает, ждёт, делает паузы.
  3. С каждой страницы вытаскивает ID карточек, которых ещё нет в базе.
  4. ЗАХОДИТ В КАЖДУЮ КАРТОЧКУ по отдельности и снимает:
       - название, цену, фото (и отпечаток фото — dhash)
       - название фабрики, сколько лет на площадке
       - 4 метрики качества продавца: доля повторных покупателей,
         балл сервиса, доля отгрузок в срок, доля хороших отзывов
       - бейджи (возврат за счёт продавца, дропшип поштучно, экспорт и т.д.)
  5. Если 1688 перекидывает на вход/капчу — НЕ ломится дальше. Записывает
     в base_1688.sqlite время блокировки, засыпает (30 мин, потом 1ч, 2ч,
     4ч, 8ч — дальше не растёт), сама пробует снова. Так за несколько дней
     наберётся точная статистика, через сколько площадка отпускает.

База НЕ должна лежать только на одном компьютере: смысл — чтобы это
работало круглосуточно на отдельном сервере, а не на компьютере Сергея.
Скрипт от этого не зависит — просто кладёт всё в файл base_1688.sqlite
рядом с собой, где бы он ни запускался.

Установка (один раз), каждую команду отдельно:
    pip install playwright
    playwright install chromium

Запуск:
    python crawler_1688.py

Остановка: закрой окно консоли. При следующем запуске продолжит с того
места, где остановился — уже собранные карточки не трогает.
"""

import os
import random
import re
import sqlite3
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Не установлен playwright. Выполни по очереди:")
    print("    pip install playwright")
    print("    playwright install chromium")
    input("Нажми Enter, чтобы закрыть...")
    raise SystemExit(1)


# ----------------------------------------------------------------------------
# НАСТРОЙКИ
# ----------------------------------------------------------------------------

# Китайские запросы, по которым собираем базу.
KEYWORDS = [
    "男士内裤",        # мужские трусы
    "男士内裤 冰丝",   # мужские трусы из ледяного шёлка
    "男生平角内裤",    # мужские боксеры
    "男士内裤 纯棉",   # мужские трусы хлопок
]

PAGES_PER_KEYWORD = 25           # сколько страниц выдачи листать на запрос
MAX_DETAILS_PER_LISTING_PAGE = 60  # сколько новых карточек открывать с одной страницы выдачи

# Паузы между обычными действиями — так ведёт себя человек, а не скрипт
PAUSE_MIN, PAUSE_MAX = 6.0, 14.0
LONG_PAUSE_EVERY = 12             # каждые N карточек/страниц — длинная пауза
LONG_PAUSE_MIN, LONG_PAUSE_MAX = 60.0, 150.0

# Бэкофф при блокировке: 30 мин, 1ч, 2ч, 4ч, 8ч — дальше не растёт
COOLDOWN_STEPS_MIN = [30, 60, 120, 240, 480]

# На своём компьютере запускай так же, окно останется открытым бесконечно —
# ничего трогать не нужно. Переменные окружения ниже нужны ТОЛЬКО для запуска
# короткими заходами на сервере (GitHub Actions и подобных) — там задаются
# автоматически из workflow-файла, вручную их выставлять не нужно.
HEADLESS = os.environ.get("CRAWLER_HEADLESS", "0") == "1"
RUN_BUDGET_SEC = int(os.environ.get("CRAWLER_RUN_BUDGET_SEC", "0"))  # 0 = без ограничения (свой компьютер)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "base_1688.sqlite")

BADGE_WORDS = [
    "退货包运费", "一件代发", "跨境", "包邮", "极速退款", "晚发必赔",
    "先采后付", "严选", "48H发货", "官方物流", "新人首单优惠",
]


# ----------------------------------------------------------------------------
# БАЗА
# ----------------------------------------------------------------------------

def open_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS products (
            offer_id       TEXT PRIMARY KEY,
            title          TEXT,
            price          REAL,
            factory        TEXT,
            years_on_site  INTEGER,
            repeat_rate    INTEGER,
            service_score  REAL,
            ontime_rate    INTEGER,
            review_rate    REAL,
            badges         TEXT,
            img_url        TEXT,
            phash          TEXT,
            keyword        TEXT,
            added_at       TEXT,
            updated_at     TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS done_pages (
            keyword TEXT,
            page    INTEGER,
            n_found INTEGER,
            done_at TEXT,
            PRIMARY KEY (keyword, page)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS block_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            block_type TEXT,
            url TEXT,
            cooldown_min INTEGER,
            consecutive_n INTEGER
        )
    """)
    con.commit()
    return con


def page_already_done(con, keyword, page):
    cur = con.execute(
        "SELECT 1 FROM done_pages WHERE keyword=? AND page=?", (keyword, page)
    )
    return cur.fetchone() is not None


def mark_page_done(con, keyword, page, n_found):
    con.execute(
        "INSERT OR REPLACE INTO done_pages VALUES (?,?,?,?)",
        (keyword, page, n_found, time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    con.commit()


def offer_already_collected(con, offer_id):
    cur = con.execute("SELECT 1 FROM products WHERE offer_id=?", (offer_id,))
    return cur.fetchone() is not None


def save_product(con, data, keyword):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    con.execute("""
        INSERT INTO products
            (offer_id, title, price, factory, years_on_site, repeat_rate,
             service_score, ontime_rate, review_rate, badges, img_url,
             phash, keyword, added_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(offer_id) DO UPDATE SET
            title=excluded.title, price=excluded.price,
            factory=excluded.factory, years_on_site=excluded.years_on_site,
            repeat_rate=excluded.repeat_rate, service_score=excluded.service_score,
            ontime_rate=excluded.ontime_rate, review_rate=excluded.review_rate,
            badges=excluded.badges, img_url=excluded.img_url,
            phash=excluded.phash, updated_at=excluded.updated_at
    """, (
        data["offer_id"], data["title"], data["price"], data["factory"],
        data["years_on_site"], data["repeat_rate"], data["service_score"],
        data["ontime_rate"], data["review_rate"], ",".join(data["badges"]),
        data["img_url"], data["phash"], keyword, stamp, stamp,
    ))
    con.commit()


def log_block(con, block_type, url, cooldown_min, consecutive_n):
    con.execute(
        "INSERT INTO block_log (ts, block_type, url, cooldown_min, consecutive_n) "
        "VALUES (?,?,?,?,?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"), block_type, url, cooldown_min, consecutive_n),
    )
    con.commit()


def still_in_cooldown(con):
    """Для коротких заходов (GitHub Actions): проверяет последнюю блокировку и
    говорит, сколько ещё минут ждать. None — можно работать прямо сейчас."""
    row = con.execute(
        "SELECT ts, cooldown_min FROM block_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    ts_str, cooldown_min = row
    blocked_at = time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M:%S"))
    elapsed_min = (time.time() - blocked_at) / 60.0
    remaining = cooldown_min - elapsed_min
    return remaining if remaining > 0 else None


# ----------------------------------------------------------------------------
# СБОР ID КАРТОЧЕК СО СТРАНИЦЫ ВЫДАЧИ (быстро, без захода внутрь)
# ----------------------------------------------------------------------------

LISTING_IDS_JS = r"""
() => {
  const all = Array.from(document.querySelectorAll('a[href]'));
  const ids = new Set();
  for (const a of all) {
    const m = a.href.match(/offer\/(\d{9,14})\.html/);
    if (m) ids.add(m[1]);
  }
  return Array.from(ids);
}
"""

# Выполняется на самой карточке товара: текст страницы + первая крупная
# фотография с отпечатком (dhash), чтобы потом сверять с фото Kaspi.
DETAIL_JS = r"""
async () => {
  function dhash(im) {
    const c = document.createElement('canvas');
    c.width = 9; c.height = 8;
    const x = c.getContext('2d');
    x.drawImage(im, 0, 0, 9, 8);
    const d = x.getImageData(0, 0, 9, 8).data;
    const g = [];
    for (let i = 0; i < 72; i++) g.push(0.299*d[i*4] + 0.587*d[i*4+1] + 0.114*d[i*4+2]);
    let b = '';
    for (let r = 0; r < 8; r++)
      for (let col = 0; col < 8; col++)
        b += (g[r*9+col] < g[r*9+col+1]) ? '1' : '0';
    return b;
  }
  function loadImg(u) {
    return new Promise(res => {
      const im = new Image();
      im.crossOrigin = 'anonymous';
      im.onload = () => res(im);
      im.onerror = () => res(null);
      setTimeout(() => res(null), 9000);
      im.src = u;
    });
  }

  const imgs = Array.from(document.querySelectorAll('img'))
      .filter(i => (i.src||'').includes('alicdn') && i.naturalWidth > 150);
  const mainImg = imgs[0] ? imgs[0].src.split('?')[0] : null;

  let hash = null;
  if (mainImg) {
    const loaded = await loadImg(mainImg);
    if (loaded) { try { hash = dhash(loaded); } catch(e) { hash = null; } }
  }

  return { text: document.body.innerText || '', title: document.title || '',
           mainImg: mainImg, hash: hash };
}
"""


def parse_detail(offer_id, raw):
    text = raw.get("text") or ""
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    def find_num(pattern, cast=int):
        m = re.search(pattern, text)
        return cast(m.group(1)) if m else None

    factory = None
    for l in lines[:6]:
        if re.search(r"(厂|公司|商行|合作社|实业|工厂店)$", l):
            factory = l
            break

    raw_title = (raw.get("title") or "").strip()
    title = re.sub(r"\s*-\s*阿里巴巴\s*$", "", raw_title) if raw_title else (lines[0] if lines else None)

    # цена может прийти разбитой по строкам: "¥\n15\n.90" — склеиваем целую и дробную часть
    price = None
    pm = re.search(r"[¥￥]\s*(\d+)\s*\.\s*(\d+)", text)
    if pm:
        price = float(pm.group(1) + "." + pm.group(2))
    else:
        pm2 = re.search(r"[¥￥]\s*(\d+(?:\.\d+)?)", text)
        if pm2:
            price = float(pm2.group(1))

    badges = [b for b in BADGE_WORDS if b in text]

    return {
        "offer_id": offer_id,
        "title": title,
        "price": price,
        "factory": factory,
        "years_on_site": find_num(r"入驻(\d+)年"),
        "repeat_rate": find_num(r"店铺回头率\s*\n?\s*(\d+)%"),
        "service_score": find_num(r"店铺服务分\s*\n?\s*([\d.]+)分", float),
        "ontime_rate": find_num(r"准时发货率\s*\n?\s*(\d+)%"),
        "review_rate": find_num(r"店铺好评率\s*\n?\s*([\d.]+)%", float),
        "badges": badges,
        "img_url": raw.get("mainImg"),
        "phash": raw.get("hash"),
    }


def looks_like_block(page):
    """Ловим и капчу, и требование войти в аккаунт (перекидывает на login)."""
    try:
        u = (page.url or "").lower()
        t = (page.title() or "").lower()
    except Exception:
        return None
    if any(m in u for m in ["punish", "x5sec", "captcha"]) or "captcha" in t:
        return "капча"
    if "login" in u or "signin" in u or "login_jump" in u:
        return "требуют вход в аккаунт"
    return None


# ----------------------------------------------------------------------------
# ПАУЗЫ И БЭКОФФ
# ----------------------------------------------------------------------------

def human_pause(counter):
    if counter and counter % LONG_PAUSE_EVERY == 0:
        t = random.uniform(LONG_PAUSE_MIN, LONG_PAUSE_MAX)
        print("      ... длинная пауза %.0f сек" % t)
    else:
        t = random.uniform(PAUSE_MIN, PAUSE_MAX)
    time.sleep(t)


class StopBurst(Exception):
    """Короткий заход (GitHub Actions) закончился раньше времени —
    либо упёрлись в блокировку, либо кончился отведённый бюджет времени.
    Это не ошибка: main() ловит её, аккуратно завершает скрипт с кодом 0,
    и следующий запуск по расписанию продолжит с того же места."""
    pass


def wait_out_block(con, block_type, url, consecutive_blocks):
    idx = min(consecutive_blocks - 1, len(COOLDOWN_STEPS_MIN) - 1)
    cooldown = COOLDOWN_STEPS_MIN[idx]
    log_block(con, block_type, url, cooldown, consecutive_blocks)
    print("\n!!! 1688 заблокировал: %s" % block_type)
    print("!!! Это блокировка №%d подряд." % consecutive_blocks)

    if RUN_BUDGET_SEC > 0:
        # Короткий заход на сервере: не спим внутри процесса — просто
        # выходим. Пауза до следующего запуска по расписанию сама сыграет
        # роль охлаждения, а still_in_cooldown() не даст начать раньше времени.
        print("!!! Засыпать на %d мин здесь не будем (короткий серверный заход) —"
              " просто выходим." % cooldown)
        print("!!! Время блокировки записано в base_1688.sqlite (таблица block_log).")
        print("!!! Следующий запуск по расписанию сам проверит, прошло ли охлаждение.")
        raise StopBurst()

    print("!!! Засыпаю на %d мин, потом сама проверю." % cooldown)
    print("!!! Время блокировки записано в base_1688.sqlite (таблица block_log) —")
    print("!!! так через несколько дней будет видно, какой ритм 1688 реально терпит.")
    print("!!! Ничего нажимать не нужно, скрипт сам проснётся и продолжит.")
    remaining = cooldown * 60
    step = 60
    while remaining > 0:
        time.sleep(min(step, remaining))
        remaining -= step
    print(">>> Пробуждаюсь, пробую снова...")


def budget_left(start_time):
    """True, пока есть время работать (для коротких серверных заходов)."""
    if RUN_BUDGET_SEC <= 0:
        return True
    return (time.time() - start_time) < RUN_BUDGET_SEC


# ----------------------------------------------------------------------------
# ОСНОВНОЙ ЦИКЛ
# ----------------------------------------------------------------------------

def main():
    con = open_db()

    total_before = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    print("=" * 70)
    print("Сборщик базы 1688 — заходит внутрь каждой карточки")
    print("В базе уже:", total_before, "товаров")
    print("Файл базы:", DB_PATH)
    print("=" * 70)

    if RUN_BUDGET_SEC > 0:
        remaining = still_in_cooldown(con)
        if remaining is not None:
            print(">>> Ещё в режиме охлаждения после блокировки — осталось ~%d мин."
                  " Пропускаю этот заход, следующий по расписанию попробует снова."
                  % remaining)
            return
        print(">>> Короткий серверный заход, бюджет времени: %d сек" % RUN_BUDGET_SEC)

    consecutive_blocks = 0
    action_counter = 0
    start_time = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            locale="ru-RU",
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/140.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()

        try:
         while True:  # бесконечный круг по всем запросам (на своём ПК); короткий заход сам выйдет по бюджету
            if not budget_left(start_time):
                raise StopBurst()
            for kw in KEYWORDS:
                print("\n### Запрос:", kw)
                for pno in range(1, PAGES_PER_KEYWORD + 1):

                    if not budget_left(start_time):
                        raise StopBurst()

                    if page_already_done(con, kw, pno):
                        continue

                    url = ("https://s.1688.com/selloffer/offer_search.htm"
                           "?charset=utf8&keywords=" + kw.replace(" ", "%20")
                           + "&beginPage=" + str(pno))

                    try:
                        page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    except Exception as e:
                        print("  стр.%-3d — не открылась (%s)" % (pno, type(e).__name__))
                        human_pause(action_counter)
                        continue

                    time.sleep(random.uniform(3.0, 5.0))

                    block = looks_like_block(page)
                    if block:
                        consecutive_blocks += 1
                        wait_out_block(con, block, page.url, consecutive_blocks)
                        continue
                    consecutive_blocks = 0

                    for _ in range(6):
                        page.mouse.wheel(0, random.randint(700, 1100))
                        time.sleep(random.uniform(0.5, 1.1))

                    try:
                        offer_ids = page.evaluate(LISTING_IDS_JS)
                    except Exception:
                        offer_ids = []

                    new_ids = [oid for oid in offer_ids if not offer_already_collected(con, oid)]
                    new_ids = new_ids[:MAX_DETAILS_PER_LISTING_PAGE]
                    print("  стр.%-3d — карточек на странице %d, новых для сбора %d"
                          % (pno, len(offer_ids), len(new_ids)))

                    for oid in new_ids:
                        if not budget_left(start_time):
                            raise StopBurst()

                        detail_url = "https://detail.1688.com/offer/%s.html" % oid
                        try:
                            page.goto(detail_url, timeout=45000, wait_until="domcontentloaded")
                        except Exception as e:
                            print("    товар %s — не открылся (%s)" % (oid, type(e).__name__))
                            human_pause(action_counter)
                            continue

                        time.sleep(random.uniform(2.0, 4.0))

                        block = looks_like_block(page)
                        if block:
                            consecutive_blocks += 1
                            wait_out_block(con, block, page.url, consecutive_blocks)
                            continue
                        consecutive_blocks = 0

                        try:
                            raw = page.evaluate(DETAIL_JS)
                            data = parse_detail(oid, raw)
                            save_product(con, data, kw)
                            total = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
                            print("    товар %s — %s | фабрика: %s | %s лет | возврат %s%% "
                                  "| сервис %s | в срок %s%% | отзывы %s%% | всего в базе %d"
                                  % (oid, (data["title"] or "?")[:30], data["factory"],
                                     data["years_on_site"], data["repeat_rate"],
                                     data["service_score"], data["ontime_rate"],
                                     data["review_rate"], total))
                        except Exception as e:
                            print("    товар %s — сбор не удался (%s)" % (oid, type(e).__name__))

                        action_counter += 1
                        human_pause(action_counter)

                    mark_page_done(con, kw, pno, len(offer_ids))
                    action_counter += 1
                    human_pause(action_counter)

            if RUN_BUDGET_SEC > 0:
                # Короткий серверный заход: прошли всё, что можно за один раз —
                # выходим, следующий запуск по расписанию продолжит.
                raise StopBurst()

            print("\n>>> Прошёл все запросы по кругу. Начинаю заново — "
                  "новые товары появляются на 1688 каждый день.")
            time.sleep(random.uniform(300, 600))

        except StopBurst:
            elapsed = time.time() - start_time
            total_now = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            print("\n" + "=" * 70)
            print(">>> Короткий заход закончен за %.0f сек. Всего в базе: %d товаров."
                  % (elapsed, total_now))
            print(">>> Это нормально — следующий запуск по расписанию продолжит")
            print(">>> ровно с того места, где остановились (уже пройденные")
            print(">>> страницы и карточки не трогает).")
            print("=" * 70)

        browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено.")
