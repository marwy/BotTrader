import os
import aiosqlite
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_history.db")

async def init_db():
    """
    Инициализирует БД: создаёт таблицы и индексы, добавляет новые колонки при необходимости.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                display_name TEXT,
                price REAL NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS successful_deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                buy_price REAL NOT NULL,
                estimated_sell_price REAL NOT NULL,
                status TEXT DEFAULT 'BOUGHT',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seller_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_name TEXT NOT NULL,
                item_id TEXT NOT NULL,
                price REAL NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Индексы
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_price_history_item_id ON price_history(item_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_seller_activity_item_id ON seller_activity(item_id)"
        )

        # Добавляем новые колонки (игнорируем если уже есть)
        try:
            await db.execute("ALTER TABLE successful_deals ADD COLUMN sell_price REAL DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE successful_deals ADD COLUMN sold_timestamp DATETIME")
        except Exception:
            pass

        # ── Отпечатки листингов (seller + item_id + price = уникальный листинг) ──
        # Считает, сколько циклов сканирования листинг был виден.
        # Высокий times_seen → листинг не продаётся → стагнирующий рынок.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS listing_fingerprints (
                fingerprint TEXT PRIMARY KEY,
                seller      TEXT NOT NULL,
                item_id     TEXT NOT NULL,
                price       REAL NOT NULL,
                first_seen  REAL NOT NULL,
                last_seen   REAL NOT NULL,
                times_seen  INTEGER DEFAULT 1
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fingerprint_item_id ON listing_fingerprints(item_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fingerprint_last_seen ON listing_fingerprints(last_seen)"
        )

        await db.commit()



async def save_price(item_id: str, display_name: str, price: float):

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO price_history (item_id, display_name, price) VALUES (?, ?, ?)",
            (item_id, display_name, price)
        )
        await db.commit()


async def get_recent_prices(item_id: str, limit: int = 50) -> list[float]:
    """
    Возвращает последние цены предмета в хронологическом порядке (от старых к новым).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT price FROM price_history WHERE item_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (item_id, limit)
        )
        rows = await cursor.fetchall()
        # Запрос по DESC → разворачиваем для хронологического порядка
        prices = [row[0] for row in rows]
        prices.reverse()
        return prices


async def save_deal(item_id: str, buy_price: float, estimated_sell_price: float) -> int:
    """
    Сохраняет сделку и возвращает её id.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO successful_deals (item_id, buy_price, estimated_sell_price) VALUES (?, ?, ?)",
            (item_id, buy_price, estimated_sell_price)
        )
        await db.commit()
        return cursor.lastrowid


async def get_active_deals_count() -> int:
    """
    Число сделок в статусе BOUGHT (предметы на продаже, ещё не проданные).
    Используется для ограничения лимита слотов продажи.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM successful_deals WHERE status = 'BOUGHT'"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0



async def save_seller_activity(seller_name: str, item_id: str, price: float):

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO seller_activity (seller_name, item_id, price) VALUES (?, ?, ?)",
            (seller_name, item_id, price)
        )
        await db.commit()


async def get_item_liquidity(item_id: str, days: int = 3) -> int:
    """
    Число записей в price_history за последние N дней. Больше = ликвиднее рынок.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await db.execute(
            "SELECT COUNT(*) FROM price_history WHERE item_id = ? AND timestamp >= ?",
            (item_id, cutoff)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_competing_prices(item_id: str, limit: int = 10) -> list[float]:
    """
    Последние уникальные цены предмета в порядке возрастания.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT price FROM price_history WHERE item_id = ? ORDER BY timestamp DESC LIMIT ?",
            (item_id, limit)
        )
        rows = await cursor.fetchall()
        prices = sorted([row[0] for row in rows])
        return prices


async def update_deal_status(deal_id: int, status: str, sell_price: float = None):
    """
    Обновляет статус сделки; при наличии sell_price — фиксирует цену и время продажи.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if sell_price is not None:
            await db.execute(
                "UPDATE successful_deals SET status = ?, sell_price = ?, sold_timestamp = CURRENT_TIMESTAMP WHERE id = ?",
                (status, sell_price, deal_id)
            )
        else:
            await db.execute(
                "UPDATE successful_deals SET status = ? WHERE id = ?",
                (status, deal_id)
            )
        await db.commit()


async def get_profit_summary(days: int = 1) -> dict:
    """
    Сводка по прибыли за последние N дней.
    Возвращает: {total_bought, total_sold, total_spent, total_earned, profit}
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        # Куплено
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(buy_price), 0) FROM successful_deals WHERE timestamp >= ?",
            (cutoff,)
        )
        row = await cursor.fetchone()
        total_bought = row[0] if row else 0
        total_spent = row[1] if row else 0.0

        # Продано
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(sell_price), 0) FROM successful_deals "
            "WHERE status = 'SOLD' AND sold_timestamp >= ?",
            (cutoff,)
        )
        row = await cursor.fetchone()
        total_sold = row[0] if row else 0
        total_earned = row[1] if row else 0.0

        return {
            "total_bought": total_bought,
            "total_sold": total_sold,
            "total_spent": float(total_spent),
            "total_earned": float(total_earned),
            "profit": float(total_earned - total_spent),
        }


async def cleanup_old_data(days: int = 7):
    """
    Удаляет записи старше N дней из price_history, seller_activity и listing_fingerprints.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute("DELETE FROM price_history WHERE timestamp < ?", (cutoff,))
        await db.execute("DELETE FROM seller_activity WHERE timestamp < ?", (cutoff,))
        # Отпечатки листингов, не виденные N дней
        import time as _time
        fp_cutoff = _time.time() - days * 86400
        await db.execute("DELETE FROM listing_fingerprints WHERE last_seen < ?", (fp_cutoff,))
        await db.commit()


async def upsert_listing_fingerprint(seller: str, item_id: str, price: float) -> int:
    """
    Фиксирует или обновляет листинг (seller + item_id + price).
    Каждый вызов = один цикл сканирования.

    Возвращает times_seen — сколько подряд циклов листинг виден по той же цене.
    Высокое значение → листинг не продаётся → стагнирующий рынок.
    """
    import time as _time
    fingerprint = f"{seller}:{item_id}:{price:.0f}"
    now = _time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO listing_fingerprints
                (fingerprint, seller, item_id, price, first_seen, last_seen, times_seen)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(fingerprint) DO UPDATE SET
                last_seen  = excluded.last_seen,
                times_seen = times_seen + 1
        """, (fingerprint, seller, item_id, price, now, now))
        await db.commit()
        async with db.execute(
            "SELECT times_seen FROM listing_fingerprints WHERE fingerprint=?", (fingerprint,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 1


async def get_listing_staleness(item_id: str, lookback_seconds: int = 86400) -> int:
    """
    Максимальный times_seen среди активных листингов предмета.

    Интерпретация:
      1–2  → свежий листинг
      3–5  → умеренная стагнация
      6–15 → вероятно никто не покупает
      15+  → почти наверняка хранилище

    Учитываются только отпечатки, виденные в течение lookback_seconds.
    """
    import time as _time
    cutoff = _time.time() - lookback_seconds
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT MAX(times_seen) FROM listing_fingerprints "
            "WHERE item_id=? AND last_seen > ?",
            (item_id, cutoff)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else 0


async def get_price_history_for_chart(search_term: str, limit: int = 50) -> list[tuple[str, float]]:
    """
    Ищет по item_id или display_name LIKE, возвращает кортежи (timestamp, price).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        like_term = f"%{search_term}%"
        cursor = await db.execute(
            "SELECT timestamp, price FROM price_history "
            "WHERE item_id = ? OR display_name LIKE ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (search_term, like_term, limit)
        )
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]


async def get_top_sellers(item_id: str = None, limit: int = 10) -> list[tuple[str, int]]:
    """
    Топ продавцов: (seller_name, count), опционально по конкретному предмету.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if item_id:
            cursor = await db.execute(
                "SELECT seller_name, COUNT(*) as cnt FROM seller_activity "
                "WHERE item_id = ? GROUP BY seller_name ORDER BY cnt DESC LIMIT ?",
                (item_id, limit)
            )
        else:
            cursor = await db.execute(
                "SELECT seller_name, COUNT(*) as cnt FROM seller_activity "
                "GROUP BY seller_name ORDER BY cnt DESC LIMIT ?",
                (limit,)
            )
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]


async def search_item_id_by_name(name_query: str) -> str | None:
    """
    Ищет item_id по display_name LIKE %query%. Возвращает наиболее частый item_id или None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        like_term = f"%{name_query}%"
        cursor = await db.execute(
            "SELECT item_id, COUNT(*) as cnt FROM price_history "
            "WHERE display_name LIKE ? GROUP BY item_id ORDER BY cnt DESC LIMIT 1",
            (like_term,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None
