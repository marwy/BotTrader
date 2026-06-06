import os
import logging
import io
import json
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.types import BufferedInputFile
from dotenv import load_dotenv

import config
import database

logger = logging.getLogger("TG-Bot")

# Загрузка переменных окружения
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

bot = None
dp = Dispatcher()

if TOKEN and CHAT_ID:
    try:
        bot = Bot(token=TOKEN)
        logger.info("Telegram Bot initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize Telegram Bot: {e}")
else:
    logger.warning("Telegram credentials not configured in .env. Bot notifications are disabled.")

# Обработчики команд
@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    help_text = (
        "🤖 <b>Minecraft Trading Bot Control Center</b>\n\n"
        "<b>🎮 Управление режимом:</b>\n"
        "📊 /status - Статус системы и баланс\n"
        "🟢 /active - Режим активной торговли (авто-покупка)\n"
        "🟡 /scanning - Режим сканирования (только уведомления)\n"
        "🔍 /scan - Запустить сканирование рынка сейчас\n"
        "⏸ /scanner on|off - Включить/отключить авто-сканирование\n"
        "⏱ /interval [минуты] - Интервал между сканами (0 = непрерывно)\n\n"
        "<b>📈 Аналитика:</b>\n"
        "💰 /profit [дни] - Отчет о прибыли (по умолч. 7 дней)\n"
        "📉 /chart &lt;название&gt; - График цен предмета\n"
        "👥 /top_sellers [название] - Топ продавцов\n\n"
        "<b>⚙️ Фильтры:</b>\n"
        "🚫 /blacklist add|remove|list &lt;item_id&gt;\n"
        "✅ /whitelist add|remove|list &lt;item_id&gt;"
    )
    await message.reply(help_text, parse_mode="HTML")

@dp.message(Command("status"))
async def cmd_status(message: Message):
    mode_emoji = "🟢 ACTIVE (Торговля)" if config.TRADING_MODE == "ACTIVE" else "🟡 SCANNING (Сканирование)"
    balance_val = f"{config.CURRENT_BALANCE:,}$" if config.CURRENT_BALANCE >= 0 else "Неизвестно"
    clients_count = len(config.CONNECTED_CLIENTS)

    # Режим сканирования
    interval = config.SCAN_INTERVAL_SECONDS
    if interval == 0:
        scan_mode = "♾️ Непрерывный (без паузы)"
    else:
        mins, secs = divmod(interval, 60)
        scan_mode = f"⏱ Таймер: каждые {'{}м {}с'.format(mins, secs) if mins else '{}с'.format(secs)}"

    # Статус следующего скана
    task = config.PENDING_SCAN_TASK
    if task and not task.done():
        next_scan = "⏳ Ожидание следующего скана..."
    elif clients_count == 0:
        next_scan = "❌ Нет подключённых клиентов"
    else:
        next_scan = "✅ Клиент подключён"

    status_text = (
        f"📋 <b>Текущий Статус Системы:</b>\n\n"
        f"⚙️ <b>Режим работы:</b> {mode_emoji}\n"
        f"🔍 <b>Режим скана:</b> {scan_mode}\n"
        f"💰 <b>Последний баланс:</b> {balance_val}\n"
        f"🔌 <b>Подключено клиентов:</b> {clients_count}\n"
        f"📡 <b>Статус скана:</b> {next_scan}\n"
    )
    await message.reply(status_text, parse_mode="HTML")

@dp.message(Command("active"))
async def cmd_active(message: Message):
    config.TRADING_MODE = "ACTIVE"
    logger.info("TRADING_MODE changed to ACTIVE via Telegram command.")
    await message.reply("🟢 Режим изменен на <b>ACTIVE</b> (Автоматическая покупка и перепродажа включены).", parse_mode="HTML")

@dp.message(Command("scanning"))
async def cmd_scanning(message: Message):
    config.TRADING_MODE = "SCANNING"
    logger.info("TRADING_MODE changed to SCANNING via Telegram command.")
    await message.reply("🟡 Режим изменен на <b>SCANNING</b> (Только сбор цен и отправка уведомлений).", parse_mode="HTML")

@dp.message(Command("scan"))
async def cmd_scan(message: Message):
    clients_count = len(config.CONNECTED_CLIENTS)
    if clients_count == 0:
        await message.reply("❌ Нет активных подключений клиента Minecraft для запуска сканирования.")
        return
        
    command = {
        "type": "ACTION_COMMAND",
        "payload": {
            "action": "OPEN_AH"
        }
    }
    command_str = json.dumps(command)
    
    success_count = 0
    # Копируем в список, чтобы избежать изменения set во время итерации
    for ws in list(config.CONNECTED_CLIENTS):
        try:
            await ws.send(command_str)
            success_count += 1
        except Exception as e:
            logger.error(f"Failed to send OPEN_AH command: {e}")
            
    await message.reply(f"🔍 Запрос открытия аукциона отправлен на клиенты ({success_count}/{clients_count}).")


@dp.message(Command("scanner"))
async def cmd_scanner(message: Message):
    """Включает или отключает авто-сканирование аукциона на клиенте Minecraft."""
    args = message.text.split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        scanner_state = "🟢 включён" if config.SCANNER_ENABLED else "🔴 отключён"
        await message.reply(
            f"⏸ Авто-сканирование сейчас: <b>{scanner_state}</b>\n\n"
            f"Использование: <code>/scanner on</code> или <code>/scanner off</code>",
            parse_mode="HTML"
        )
        return

    enable = args[1].lower() == "on"
    config.SCANNER_ENABLED = enable

    command = {
        "type": "ACTION_COMMAND",
        "payload": {
            "action": "TOGGLE_SCANNER",
            "enabled": enable
        }
    }
    command_str = json.dumps(command)

    clients_count = len(config.CONNECTED_CLIENTS)
    success_count = 0
    for ws in list(config.CONNECTED_CLIENTS):
        try:
            await ws.send(command_str)
            success_count += 1
        except Exception as e:
            logger.error(f"Failed to send TOGGLE_SCANNER command: {e}")

    status = "🟢 включён" if enable else "🔴 отключён"
    await message.reply(
        f"⏸ Авто-сканирование <b>{status}</b>. Команда отправлена на {success_count}/{clients_count} клиентов.",
        parse_mode="HTML"
    )

@dp.message(Command("interval"))
async def cmd_interval(message: Message):
    """Устанавливает интервал сканирования в минутах. 0 = непрерывный, >0 = таймер."""
    args = message.text.split()

    # Без аргументов — показываем текущую настройку
    if len(args) < 2:
        current = config.SCAN_INTERVAL_SECONDS
        if current == 0:
            text = (
                "⏱ <b>Интервал сканирования:</b> <code>♾️ Непрерывный</code>\n\n"
                "Бот сразу открывает /ah после каждого цикла.\n\n"
                "Использование: <code>/interval 10</code> — пауза 10 минут\n"
                "<code>/interval 0</code> — вернуть непрерывный режим"
            )
        else:
            mins = current // 60
            secs = current % 60
            text = (
                f"⏱ <b>Интервал сканирования:</b> <code>{mins}м {secs}с</code>\n\n"
                f"Бот делает паузу {mins}мин между циклами.\n\n"
                "Использование: <code>/interval 0</code> — непрерывный режим\n"
                "<code>/interval 5</code> — пауза 5 минут"
            )
        await message.reply(text, parse_mode="HTML")
        return

    # Парсим новый интервал
    try:
        minutes = float(args[1])
        if minutes < 0:
            raise ValueError
    except ValueError:
        await message.reply("❌ Укажите число минут >= 0. Например: <code>/interval 10</code>", parse_mode="HTML")
        return

    new_seconds = int(minutes * 60)
    config.SCAN_INTERVAL_SECONDS = new_seconds
    logger.info(f"SCAN_INTERVAL_SECONDS changed to {new_seconds}s via Telegram.")

    # Отменяем активный таймер и пересчитываем немедленно
    if config.PENDING_SCAN_TASK and not config.PENDING_SCAN_TASK.done():
        config.PENDING_SCAN_TASK.cancel()
        config.PENDING_SCAN_TASK = None
        logger.info("Pending scan task cancelled due to interval change.")

    if new_seconds == 0:
        await message.reply(
            "♾️ Режим изменён на <b>Непрерывный</b> — бот сразу открывает /ah после каждого цикла.",
            parse_mode="HTML"
        )
    else:
        mins = new_seconds // 60
        secs = new_seconds % 60
        label = f"{mins}м {secs}с" if secs else f"{mins}м"
        await message.reply(
            f"⏱ Режим изменён на <b>Таймер {label}</b> — пауза между циклами сканирования.\n\n"
            f"Текущий цикл завершится как обычно, следующий запустится через {label}.",
            parse_mode="HTML"
        )


@dp.message(Command("profit"))
async def cmd_profit(message: Message):
    """Отчёт о прибыли за указанное число дней."""
    args = message.text.split()
    days = 7  # default
    if len(args) > 1:
        try:
            days = int(args[1])
            if days < 1:
                days = 1
            elif days > 365:
                days = 365
        except ValueError:
            await message.reply("❌ Укажите число дней, например: <code>/profit 7</code>", parse_mode="HTML")
            return

    try:
        summary = await database.get_profit_summary(days)
        profit_emoji = "📈" if summary["profit"] >= 0 else "📉"
        
        text = (
            f"💰 <b>Отчет о прибыли за {days} дн.</b>\n\n"
            f"🛒 <b>Куплено:</b> {summary['total_bought']} шт.\n"
            f"🏷️ <b>Продано:</b> {summary['total_sold']} шт.\n"
            f"💸 <b>Потрачено:</b> {summary['total_spent']:,.0f}$\n"
            f"💵 <b>Заработано:</b> {summary['total_earned']:,.0f}$\n"
            f"{profit_emoji} <b>Чистая прибыль:</b> {summary['profit']:,.0f}$"
        )
        await message.reply(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error in /profit command: {e}", exc_info=True)
        await message.reply("❌ Ошибка при получении данных о прибыли.")

@dp.message(Command("chart"))
async def cmd_chart(message: Message):
    """Генерирует и отправляет график истории цен предмета."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("❌ Укажите название предмета, например: <code>/chart Diamond Sword</code>", parse_mode="HTML")
        return
    
    search_term = args[1].strip()
    
    try:
        # Пробуем найти item_id по названию
        item_id = await database.search_item_id_by_name(search_term)
        if not item_id:
            # Используем search_term напрямую как item_id
            item_id = search_term
        
        history = await database.get_price_history_for_chart(item_id, limit=50)
        if not history or len(history) < 2:
            await message.reply(f"❌ Недостаточно данных для построения графика по запросу: <code>{search_term}</code>", parse_mode="HTML")
            return
        
        # Генерируем график
        chart_bytes = generate_history_chart_bytes(search_term, history)
        if chart_bytes:
            input_file = BufferedInputFile(chart_bytes, filename="chart.png")
            await message.reply_photo(
                photo=input_file,
                caption=f"📉 <b>История цен:</b> {search_term}\n📊 Записей: {len(history)}",
                parse_mode="HTML"
            )
        else:
            await message.reply("❌ Не удалось сгенерировать график (matplotlib не установлен).")
    except Exception as e:
        logger.error(f"Error in /chart command: {e}", exc_info=True)
        await message.reply("❌ Ошибка при построении графика.")

@dp.message(Command("blacklist"))
async def cmd_blacklist(message: Message):
    """Управление чёрным списком: add, remove, list."""
    args = message.text.split()
    
    if len(args) < 2:
        text = (
            "🚫 <b>Управление черным списком</b>\n\n"
            "<code>/blacklist add &lt;item_id&gt;</code> — добавить предмет\n"
            "<code>/blacklist remove &lt;item_id&gt;</code> — удалить предмет\n"
            "<code>/blacklist list</code> — показать список"
        )
        await message.reply(text, parse_mode="HTML")
        return
    
    subcommand = args[1].lower()
    
    if subcommand == "add" and len(args) >= 3:
        item_id = args[2]
        config.BLACKLIST.add(item_id)
        await message.reply(f"🚫 <code>{item_id}</code> добавлен в черный список.", parse_mode="HTML")
    elif subcommand == "remove" and len(args) >= 3:
        item_id = args[2]
        config.BLACKLIST.discard(item_id)
        await message.reply(f"✅ <code>{item_id}</code> удален из черного списка.", parse_mode="HTML")
    elif subcommand == "list":
        if config.BLACKLIST:
            items_text = "\n".join(f"  • <code>{item}</code>" for item in sorted(config.BLACKLIST))
            text = f"🚫 <b>Черный список ({len(config.BLACKLIST)} шт.):</b>\n{items_text}"
        else:
            text = "🚫 <b>Черный список пуст.</b>"
        await message.reply(text, parse_mode="HTML")
    else:
        await message.reply("❌ Неизвестная подкоманда. Используйте: <code>add</code>, <code>remove</code>, <code>list</code>", parse_mode="HTML")

@dp.message(Command("whitelist"))
async def cmd_whitelist(message: Message):
    """Управление белым списком: add, remove, list."""
    args = message.text.split()
    
    if len(args) < 2:
        text = (
            "✅ <b>Управление белым списком</b>\n\n"
            "<code>/whitelist add &lt;item_id&gt;</code> — добавить предмет\n"
            "<code>/whitelist remove &lt;item_id&gt;</code> — удалить предмет\n"
            "<code>/whitelist list</code> — показать список\n\n"
            "ℹ️ Если список пуст — все предметы разрешены."
        )
        await message.reply(text, parse_mode="HTML")
        return
    
    subcommand = args[1].lower()
    
    if subcommand == "add" and len(args) >= 3:
        item_id = args[2]
        config.WHITELIST.add(item_id)
        await message.reply(f"✅ <code>{item_id}</code> добавлен в белый список.", parse_mode="HTML")
    elif subcommand == "remove" and len(args) >= 3:
        item_id = args[2]
        config.WHITELIST.discard(item_id)
        await message.reply(f"🗑️ <code>{item_id}</code> удален из белого списка.", parse_mode="HTML")
    elif subcommand == "list":
        if config.WHITELIST:
            items_text = "\n".join(f"  • <code>{item}</code>" for item in sorted(config.WHITELIST))
            text = f"✅ <b>Белый список ({len(config.WHITELIST)} шт.):</b>\n{items_text}"
        else:
            text = "✅ <b>Белый список пуст</b> (все предметы разрешены)."
        await message.reply(text, parse_mode="HTML")
    else:
        await message.reply("❌ Неизвестная подкоманда. Используйте: <code>add</code>, <code>remove</code>, <code>list</code>", parse_mode="HTML")

@dp.message(Command("top_sellers"))
async def cmd_top_sellers(message: Message):
    """Топ продавцов, опционально по названию предмета."""
    args = message.text.split(maxsplit=1)
    item_id = None
    filter_label = "все предметы"
    
    if len(args) > 1:
        search_term = args[1].strip()
        item_id = await database.search_item_id_by_name(search_term)
        if not item_id:
            item_id = search_term
        filter_label = search_term
    
    try:
        sellers = await database.get_top_sellers(item_id=item_id, limit=10)
        if not sellers:
            await message.reply(f"❌ Нет данных о продавцах для: <code>{filter_label}</code>", parse_mode="HTML")
            return
        
        lines = []
        for i, (seller_name, count) in enumerate(sellers, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            lines.append(f"  {medal} <b>{seller_name}</b> — {count} листингов")
        
        text = f"👥 <b>Топ продавцов</b> ({filter_label}):\n\n" + "\n".join(lines)
        await message.reply(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error in /top_sellers command: {e}", exc_info=True)
        await message.reply("❌ Ошибка при получении данных о продавцах.")


def generate_chart_bytes(item_name: str, current_price: float, estimated_sell: float, historical_prices: list) -> bytes:
    """
    Строит тёмный торговый график для уведомления о сделке.

    Особенности:
    - Дедупликация подряд идущих одинаковых цен (убирает шум повторных сканов)
    - Сглаживание скользящим средним (окно 3)
    - Градиентная заливка под кривой цены
    - Полоса доверия min/max (в стиле Боллинджера)
    - Маркер покупки (красная звезда) и цель продажи (зелёная пунктирная линия)
    """
    try:
        from matplotlib.figure import Figure
        from matplotlib.patches import FancyArrowPatch
        import numpy as np
    except ImportError:
        logger.warning("matplotlib/numpy not installed. Price charts cannot be generated.")
        return None

    if not historical_prices:
        return None

    # ── 1. Дедупликация подряд идущих одинаковых цен ────────────────────────
    # Если цена повторялась 10 раз подряд (один листинг, несколько сканов),
    # оставляем только первое и последнее вхождение.
    deduped = [historical_prices[0]]
    for p in historical_prices[1:]:
        if abs(p - deduped[-1]) / max(deduped[-1], 1) > 0.005:  # изменение > 0.5%
            deduped.append(p)
    # Добавляем текущую цену покупки в конец
    prices = deduped + [current_price]

    # ── 2. Сглаживание скользящим средним (окно 3, только при достаточном числе точек) ──
    if len(prices) >= 5:
        kernel = np.ones(3) / 3
        smoothed = np.convolve(prices, kernel, mode='same').tolist()
        # Первая и последняя точки без сглаживания (избегаем краевых артефактов)
        smoothed[0]  = prices[0]
        smoothed[-1] = prices[-1]
    else:
        smoothed = prices

    x = list(range(1, len(smoothed) + 1))

    # ── 3. Настройка фигуры (тёмная тема) ───────────────────────────────────
    BG       = "#0f1117"
    SURFACE  = "#1a1d27"
    GRID     = "#2a2d3a"
    LINE     = "#5b8dee"
    FILL     = "#3a5bc7"
    BUY_C    = "#ff4d6d"
    SELL_C   = "#2dd4bf"
    TEXT     = "#e2e8f0"
    MED_TEXT = "#94a3b8"

    fig = Figure(figsize=(9, 4.5), facecolor=BG)
    ax  = fig.subplots()
    ax.set_facecolor(SURFACE)

    for spine in ax.spines.values():
        spine.set_edgecolor(GRID)

    # ── 4. Полоса доверия (min/max сырых цен) ────────────────────────────────
    mn = min(prices)
    mx = max(prices)
    ax.fill_between(x, mn, mx, color=FILL, alpha=0.08, linewidth=0)

    # ── 5. Градиентная заливка под кривой ───────────────────────────────────
    ax.fill_between(x, smoothed, mn, color=FILL, alpha=0.22, linewidth=0)

    # ── 6. Линия цены ─────────────────────────────────────────────────────────
    ax.plot(x, smoothed, color=LINE, linewidth=2.2, zorder=4, solid_capstyle='round')

    # Точки на каждом наблюдении
    ax.scatter(x, smoothed, color=LINE, s=20, zorder=5, alpha=0.6)

    # ── 7. Маркер цены покупки ─────────────────────────────────────────────
    ax.scatter([len(x)], [current_price],
               marker='*', color=BUY_C, s=220, zorder=6, label=f'Покупка: {current_price:,.0f}$')
    ax.annotate(f'{current_price:,.0f}$',
                xy=(len(x), current_price),
                xytext=(-8, 10), textcoords='offset points',
                color=BUY_C, fontsize=8, fontweight='bold', ha='right')

    # ── 8. Линия цели продажи ────────────────────────────────────────────────
    ax.axhline(y=estimated_sell, color=SELL_C, linestyle='--',
               linewidth=1.6, alpha=0.85, zorder=3,
               label=f'Цель продажи: {estimated_sell:,.0f}$')

    # Аннотация прибыли
    profit_pct = (estimated_sell / current_price - 1) * 100 if current_price > 0 else 0
    ax.annotate(f'+{profit_pct:.1f}%',
                xy=(1, estimated_sell),
                xytext=(6, 4), textcoords='offset points',
                color=SELL_C, fontsize=8, fontweight='bold')

    # ── 9. Линия медианы ──────────────────────────────────────────────────────
    med = sorted(prices)[len(prices) // 2]
    ax.axhline(y=med, color=MED_TEXT, linestyle=':', linewidth=1.0, alpha=0.5,
               label=f'Медиана: {med:,.0f}$')

    # ── 10. Подписи и форматирование ─────────────────────────────────────────
    ax.set_title(item_name, color=TEXT, fontsize=11, fontweight='bold', pad=10)
    ax.set_xlabel('Наблюдения (дедупликация скан-дублей)', color=MED_TEXT, fontsize=8)
    ax.set_ylabel('Цена ($)', color=MED_TEXT, fontsize=8)
    ax.tick_params(colors=MED_TEXT, labelsize=8)
    ax.yaxis.set_major_formatter(
        __import__('matplotlib').ticker.FuncFormatter(lambda v, _: f'{v:,.0f}')
    )
    ax.grid(True, color=GRID, linewidth=0.8, linestyle='-', alpha=0.6)

    legend = ax.legend(loc='best', fontsize=8, framealpha=0.35,
                       facecolor=SURFACE, edgecolor=GRID, labelcolor=TEXT)

    fig.tight_layout(pad=1.2)

    buf = __import__('io').BytesIO()
    fig.savefig(buf, format='png', dpi=110, facecolor=BG)
    buf.seek(0)
    return buf.getvalue()


def generate_history_chart_bytes(item_name: str, history: list[tuple[str, float]]) -> bytes:
    """
    Строит график по кортежам (timestamp, price) из get_price_history_for_chart().
    Используется командой /chart.
    """
    try:
        from matplotlib.figure import Figure
        from matplotlib.dates import DateFormatter
        from datetime import datetime
    except ImportError:
        logger.warning("matplotlib is not installed. Price charts cannot be generated.")
        return None
    
    if not history:
        return None
    
    # БД возвращает от новых к старым — разворачиваем для хронологии
    history_sorted = list(reversed(history))
    
    timestamps = []
    prices = []
    for ts, price in history_sorted:
        try:
            if isinstance(ts, str):
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            else:
                dt = ts
            timestamps.append(dt)
            prices.append(price)
        except (ValueError, TypeError):
            continue
    
    if len(prices) < 2:
        return None
    
    fig = Figure(figsize=(10, 5))
    ax = fig.subplots()
    
    ax.plot(timestamps, prices, marker='o', color='#3498db', linewidth=2, markersize=4, label='Цена')
    
    # Маркеры минимума и максимума
    min_price = min(prices)
    max_price = max(prices)
    min_idx = prices.index(min_price)
    max_idx = prices.index(max_price)
    ax.plot(timestamps[min_idx], min_price, marker='v', color='#e74c3c', markersize=10, label=f'Мин: {min_price:,.0f}$', linestyle='None')
    ax.plot(timestamps[max_idx], max_price, marker='^', color='#2ecc71', markersize=10, label=f'Макс: {max_price:,.0f}$', linestyle='None')
    
    # Линия среднего
    avg_price = sum(prices) / len(prices)
    ax.axhline(y=avg_price, color='#f39c12', linestyle='--', linewidth=1.5, alpha=0.7, label=f'Среднее: {avg_price:,.0f}$')
    
    ax.set_title(f"История цен: {item_name}", fontsize=12, fontweight='bold', pad=10)
    ax.set_xlabel("Время", fontsize=10)
    ax.set_ylabel("Цена ($)", fontsize=10)
    ax.legend(loc='best', fontsize=8)
    ax.grid(True, linestyle=':', alpha=0.6)
    
    # Форматирование дат на оси X
    ax.xaxis.set_major_formatter(DateFormatter('%m/%d %H:%M'))
    fig.autofmt_xdate(rotation=30)
    
    fig.tight_layout()
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100)
    buf.seek(0)
    return buf.getvalue()


async def send_deal_notification(
    item_name: str,
    buy_price: float,
    estimated_sell: float,
    z_score: float,
    item_id: str = "",
    historical_prices: list = None,
    is_sniper: bool = False,
    warning: str = None,
    mode: str = "SCANNING",
    expires_in_seconds: int = -1,
    listing_staleness: int = 0,
):
    """
    Отправляет форматированное уведомление о сделке в Telegram с графиком цен.

    Параметры:
        is_sniper          – True при снайперском сигнале (глубокий дисконт)
        mode               – "ACTIVE" (бот купил) или "SCANNING" (только инфо)
        expires_in_seconds – секунд до истечения листинга из тултипа (-1 = неизвестно)
        listing_staleness  – сколько циклов сканирования этот отпечаток виден
        warning            – доп. предупреждение (низкий баланс и т.п.)
    """
    if bot is None or not CHAT_ID:
        logger.debug("Telegram Bot not configured. Skipping notification.")
        return

    # ── Метка предмета ───────────────────────────────────────────────────────
    item_label = item_name
    if item_id:
        parts = item_id.split(":")
        if len(parts) >= 3:
            enchant_raw = ",".join(parts[2:]).replace("_", " ").replace(",", ", ")
            item_label = f"{item_name} ({enchant_raw})"

    # ── Заголовок ────────────────────────────────────────────────────────────
    deal_emoji  = "🎯" if is_sniper else "📊"
    deal_type   = "SNIPER" if is_sniper else "CONSENSUS"
    mode_badge  = "🟢 КУПЛЕНО" if mode == "ACTIVE" else "🟡 СКАНИРОВАНИЕ"

    profit_abs = estimated_sell - buy_price
    profit_pct = (estimated_sell / buy_price - 1.0) * 100 if buy_price > 0 else 0
    profit_sign = "+" if profit_abs >= 0 else ""

    message = (
        f"{deal_emoji} <b>[{deal_type}] {item_label}</b>\n"
        f"<code>{mode_badge}</code>\n\n"
    )

    # ── Блок предупреждения (только при реальной проблеме) ─────────────────
    if warning:
        message += f"{warning}\n\n"

    # ── Блок цен ──────────────────────────────────────────────────────────────
    message += (
        f"💰 <b>Покупка:</b>  {buy_price:,.0f}$\n"
        f"📈 <b>Продажа:</b>  {estimated_sell:,.0f}$\n"
        f"💵 <b>Прибыль:</b>  {profit_sign}{profit_abs:,.0f}$ "
        f"(<b>{profit_sign}{profit_pct:.1f}%</b>)\n"
        f"📊 <b>Z-Score:</b>  {z_score:.2f}\n"
    )

    # ── Свежесть листинга ────────────────────────────────────────────────────
    freshness_parts = []
    if expires_in_seconds >= 0:
        if expires_in_seconds >= 3600:
            h = expires_in_seconds // 3600
            m = (expires_in_seconds % 3600) // 60
            freshness_parts.append(f"⏱ Истекает через: {h}ч {m}мин")
        elif expires_in_seconds >= 60:
            m = expires_in_seconds // 60
            freshness_parts.append(f"⏱ Истекает через: {m}мин")
        else:
            freshness_parts.append(f"⏱ Истекает через: {expires_in_seconds}сек")
    if listing_staleness > 0:
        freshness_parts.append(f"🔁 Видели {listing_staleness}× в этом цикле")
    if freshness_parts:
        message += "\n" + "\n".join(freshness_parts) + "\n"

    # ── ID предмета ───────────────────────────────────────────────────────────
    if item_id:
        message += f"\n🆔 <code>{item_id}</code>"

    # ── График ────────────────────────────────────────────────────────────────
    photo_bytes = None
    if historical_prices and len(historical_prices) > 0:
        try:
            photo_bytes = generate_chart_bytes(item_label, buy_price, estimated_sell, historical_prices)
        except Exception as ex:
            logger.error(f"Failed to generate price chart: {ex}")

    try:
        if photo_bytes:
            input_file = BufferedInputFile(photo_bytes, filename="chart.png")
            await bot.send_photo(chat_id=CHAT_ID, photo=input_file, caption=message, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="HTML")
        logger.info(f"Telegram notification sent for {item_label} ({deal_type}, {mode}).")
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")


async def send_daily_report():
    """
    Ежедневный сводный отчёт в Telegram со статистикой прибыли и состоянием системы.
    Вызывается из daily_report_task() в server.py.
    """
    if bot is None or not CHAT_ID:
        logger.debug("Telegram Bot not configured. Skipping daily report.")
        return
    
    try:
        summary = await database.get_profit_summary(1)
        profit_emoji = "📈" if summary["profit"] >= 0 else "📉"
        balance_val = f"{config.CURRENT_BALANCE:,}$" if config.CURRENT_BALANCE >= 0 else "Неизвестно"
        mode_emoji = "🟢 ACTIVE" if config.TRADING_MODE == "ACTIVE" else "🟡 SCANNING"
        clients_count = len(config.CONNECTED_CLIENTS)
        
        text = (
            f"📊 <b>Ежедневный отчет</b>\n\n"
            f"🛒 <b>Куплено за сутки:</b> {summary['total_bought']} шт.\n"
            f"🏷️ <b>Продано за сутки:</b> {summary['total_sold']} шт.\n"
            f"💸 <b>Потрачено:</b> {summary['total_spent']:,.0f}$\n"
            f"💵 <b>Заработано:</b> {summary['total_earned']:,.0f}$\n"
            f"{profit_emoji} <b>Чистая прибыль:</b> {summary['profit']:,.0f}$\n\n"
            f"<b>Состояние системы:</b>\n"
            f"⚙️ Режим: {mode_emoji}\n"
            f"💰 Баланс: {balance_val}\n"
            f"🔌 Подключено клиентов: {clients_count}"
        )
        
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
        logger.info("Daily report sent to Telegram.")
    except Exception as e:
        logger.error(f"Failed to send daily report: {e}", exc_info=True)


async def start_polling_bot():
    """
    Запускает polling диспетчера Telegram-бота в фоне.
    """
    if bot is not None:
        logger.info("Starting Telegram Bot command dispatcher polling...")
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logger.error(f"Error in Telegram Bot dispatcher: {e}")
