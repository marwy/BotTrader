import os
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TRADING_MODE = os.getenv("TRADING_MODE", "ACTIVE").upper()
CURRENT_BALANCE = -1
CONNECTED_CLIENTS = set()  # активные WebSocket-подключения
RECENT_ALERTS = {}         # (item_id, price) -> timestamp; дедупликация Telegram-уведомлений
SCANNER_ENABLED = True     # флаг паузы/возобновления авто-сканирования

# Фильтры предметов
BLACKLIST = set()  # item_id для игнорирования (например, "minecraft:tnt")
WHITELIST = set()  # только эти item_id (пусто = все разрешены)

# ── Тайминг сканирования ──────────────────────────────────────────────────────
# SCAN_INTERVAL_SECONDS:
#   0   = НЕПРЕРЫВНЫЙ — /ah открывается сразу после каждого цикла (максимум скорости)
#   >0  = ТАЙМЕР      — пауза N секунд между циклами (осторожный режим)
# Меняется в runtime через Telegram /interval.
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "0"))

# asyncio.Task следующего запланированного скана (TIMED режим).
# Отменяется при смене интервала или отключении бота.
PENDING_SCAN_TASK = None

# ── Пороги торгового анализа ─────────────────────────────────────────────────

# Снайпер: мгновенная покупка если цена < SNIPER_THRESHOLD × медиана(filtered_history)
SNIPER_THRESHOLD = 0.30

# Минимальная маржа перед сигналом покупки (применяется ко всем типам сделок).
# 0.15 = минимум 15% прибыли после перепродажи.
MIN_PROFIT_MARGIN = 0.15

# Минимум исторических точек для любого анализа.
# При меньшем количестве нет надёжной референсной цены — пропускаем предмет.
MIN_DATA_POINTS = 5

# Минимум точек для полного анализа (Bollinger/RSI/MACD).
# При меньшем — только снайперский режим.
FULL_ANALYSIS_MIN = 15

# ── База данных ──────────────────────────────────────────────────────────────
DB_RETENTION_DAYS = 7

# Час отправки ежедневного отчёта (24ч, серверный часовой пояс)
DAILY_REPORT_HOUR = 9
