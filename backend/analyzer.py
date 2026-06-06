import logging
import statistics
import math
import pandas as pd
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator
from ta.trend import MACD

import config

logger = logging.getLogger("Analyzer")

# Максимальное expires_in_seconds за всё время работы.
# Позволяет автоматически определить максимальный срок листинга на сервере.
# Пример: «6д 23ч 59м» = 604740с → максимум ≈ 7 дней.
_expiry_tracker: dict = {"max_seen": 0}


# ── Фильтрация выбросов ──────────────────────────────────────────────────────

def _filter_outliers_iqr(prices: list[float]) -> list[float]:
    """
    Удаление выбросов по IQR. Надёжнее метода 3×min:
    - 3×min ломается при листинге за 1$ (потолок становится 3$)
    - IQR привязан к центру распределения, а не к экстремумам

    Верхняя граница = Q3 + 2.0 × IQR  (отсекает тролль-цены и «хранилищные» листинги)
    Нижняя граница = max(0, Q1 - 2.0 × IQR)  (отсекает подозрительно дешёвые цены)
    """
    if len(prices) < 4:
        return prices

    sorted_p = sorted(prices)
    n = len(sorted_p)
    q1 = sorted_p[n // 4]
    q3 = sorted_p[(3 * n) // 4]
    iqr = q3 - q1

    # IQR = 0 (все цены одинаковые) — верхний порог 2× медиана
    if iqr == 0:
        median_val = sorted_p[n // 2]
        if median_val > 0:
            filtered = [p for p in prices if p <= median_val * 2.0]
            return filtered if len(filtered) >= 3 else prices
        return prices

    upper = q3 + 2.0 * iqr
    lower = max(q1 - 2.0 * iqr, 0.0)

    filtered = [p for p in prices if lower <= p <= upper]
    # Минимум 3 цены — иначе ломается статистика ниже по пайплайну
    return filtered if len(filtered) >= 3 else prices


# ── Снайперская проверка ─────────────────────────────────────────────────────

def is_sniper_deal(current_price: float, filtered_prices: list[float]) -> bool:
    """
    Вызывать только с уже отфильтрованными ценами.
    True, если current_price < SNIPER_THRESHOLD × медиана.
    """
    if len(filtered_prices) < 5:
        return False
    median_price = statistics.median(filtered_prices)
    return current_price < config.SNIPER_THRESHOLD * median_price


# ── Справедливая цена на основе EMA ─────────────────────────────────────────

def _ema_fair_value(prices: list[float]) -> float:
    """
    Справедливая цена, взвешенная по EMA.
    Свежие цены получают экспоненциально больший вес.
    span = min(len, 10): последние ~10 наблюдений доминируют.

    Критично при малом объёме данных: цена 2 часа назад
    важнее, чем вчерашняя.
    """
    s = pd.Series(prices, dtype=float)
    span = min(len(prices), 10)
    return float(s.ewm(span=span, adjust=False).mean().iloc[-1])


# ── Основная функция анализа ─────────────────────────────────────────────────

def should_buy(
    item_id: str,
    current_price: float,
    historical_prices: list[float],
    competing_prices: list[float] = None,
    listing_staleness: int = 0,
    expires_in_seconds: int = -1,
) -> tuple[bool, float, float, bool]:
    """
    Решает, покупать ли предмет.

    Пайплайн (порядок важен):
      1. Чёрный / белый список
      2. Фильтрация выбросов IQR         ← до снайперской проверки
      3. Детекция стагнирующего рынка  ─ ПРИОРИТЕТ ──────────────────────────
             a) expires_in_seconds  ← лучший сигнал: абсолютное время с сервера
             b) listing_staleness   ← счётчик циклов сканирования
             c) CV fallback         ← статистический (крайний случай)
      4. Снайперская проверка на отфильтрованных ценах
      5. Путь малых данных (5–14 точек): только снайпер, консервативно
      6. Полный анализ (15+ точек): Bollinger + RSI + MACD + EMA
      7. Целевая цена продажи (консервативно: min(BB-mid, EMA))
      8. Штраф за стагнирующий рынок
      9. Умный андеркат по живым конкурирующим ценам + проверка спреда
     10. Минимальная маржа прибыли  ← применяется ко всем типам сделок

    Args:
        listing_staleness:   times_seen из listing_fingerprints (счётчик циклов).
        expires_in_seconds:  секунд до истечения листинга (из lore предмета).
                             -1 = недоступно.

    Returns: (should_buy, estimated_sell_price, z_score, is_sniper)
    """


    # ── 1. Чёрный / белый список ─────────────────────────────────────────────
    if item_id in config.BLACKLIST:
        return (False, 0.0, 0.0, False)
    if config.WHITELIST and item_id not in config.WHITELIST:
        return (False, 0.0, 0.0, False)

    # ── 2. Фильтрация выбросов IQR (до снайперской проверки) ───────────────
    historical_prices = _filter_outliers_iqr(historical_prices)

    # ── 3. Детекция стагнирующего рынка ──────────────────────────────────────
    #
    # УРОВЕНЬ 1 (лучший): expires_in_seconds — абсолютное время с сервера.
    #   Отслеживаем максимально виденное значение для оценки длительности листинга.
    #   age_ratio = 1 - (expires_remaining / max_duration)
    #   age_ratio близко к 1.0 = листинг почти истёк = висит давно = стагнация.
    #
    # УРОВЕНЬ 2: listing_staleness — сколько циклов сканирования листинг не менялся.
    #   Не зависит от expiry, работает при ошибках парсинга времени.
    #
    # УРОВЕНЬ 3 (запасной): CV истории цен — если всё одинаково = стагнация.
    #
    stale_market = False
    stale_reason = ""

    # ── УРОВЕНЬ 1: Время истечения ────────────────────────────────────────────
    if expires_in_seconds >= 0:
        # Запоминаем максимально виденное время для оценки длительности листинга.
        # Пример: «6д 23ч» = 604200с → max_duration ≈ 604800 (7 дней)
        if expires_in_seconds > _expiry_tracker["max_seen"]:
            _expiry_tracker["max_seen"] = expires_in_seconds
            logger.debug(f"New max listing duration observed: {expires_in_seconds // 3600}h")

        max_duration = _expiry_tracker["max_seen"]

        if max_duration > 0:
            age_ratio = 1.0 - (expires_in_seconds / max_duration)
            # age_ratio > 0.90 → использовано >90% срока → сильная стагнация
            # age_ratio > 0.70 → истекло >70% → вероятная стагнация
            if age_ratio > 0.90:
                stale_market = True
                stale_reason = f"expiry={expires_in_seconds//3600}h left, age={age_ratio*100:.0f}% elapsed (VERY STALE)"
            elif age_ratio > 0.70:
                stale_market = True
                stale_reason = f"expiry={expires_in_seconds//3600}h left, age={age_ratio*100:.0f}% elapsed (stale)"
        elif expires_in_seconds < 3600:
            # max_duration ещё не известен: < 1ч остатка = стагнация
            stale_market = True
            stale_reason = f"expiry < 1h ({expires_in_seconds}s) — listing very old"

        if stale_market:
            logger.debug(f"[{item_id}] Stale via EXPIRY: {stale_reason}")
            if competing_prices is not None and len(competing_prices) <= 1:
                logger.debug(f"[{item_id}] Stale + single competing listing → skip.")
                return (False, 0.0, 0.0, False)

    # ── УРОВЕНЬ 2: Счётчик циклов по отпечатку ──────────────────────────────
    if not stale_market:
        STALE_CYCLES = 5
        if listing_staleness >= STALE_CYCLES:
            stale_market = True
            stale_reason = f"отпечаток виден {listing_staleness} циклов без изменений"
            logger.debug(f"[{item_id}] Stale via FINGERPRINT: {stale_reason}")
            if competing_prices is not None and len(competing_prices) <= 1:
                logger.debug(f"[{item_id}] Stale + single competing listing → skip.")
                return (False, 0.0, 0.0, False)

    # ── УРОВЕНЬ 3: CV-запасной (нет expiry и нет данных отпечатка) ─────────
    if not stale_market and expires_in_seconds < 0 and listing_staleness == 0:
        if len(historical_prices) >= 5:
            h_mean = statistics.mean(historical_prices)
            h_std  = statistics.stdev(historical_prices) if len(historical_prices) > 1 else 0.0
            cv = h_std / h_mean if h_mean > 0 else 0.0
            if cv < 0.05:
                stale_market = True
                stale_reason = f"CV={cv:.3f} (все цены одинаковые)"
                logger.debug(f"[{item_id}] Stale via CV fallback: {stale_reason}")
                if competing_prices is not None and len(competing_prices) <= 1:
                    logger.debug(f"[{item_id}] CV-stale + single competing listing → skip.")
                    return (False, 0.0, 0.0, False)

    # ── 4. Снайперская проверка на отфильтрованных ценах ────────────────────
    sniper = is_sniper_deal(current_price, historical_prices)

    # ── Минимальный порог данных ──────────────────────────────────────────────
    if len(historical_prices) < config.MIN_DATA_POINTS:
        logger.debug(f"Skipping {item_id}: only {len(historical_prices)} data points (min={config.MIN_DATA_POINTS}).")
        return (False, 0.0, 0.0, False)

    # ── 5. Путь малых данных (5–14 точек) ───────────────────────────────────
    # Bollinger на малом объёме ненадёжен — используем EMA и медиану.
    if len(historical_prices) < config.FULL_ANALYSIS_MIN:
        if not sniper:
            return (False, 0.0, 0.0, False)

        # При малом объёме данных требуем более глубокий дисконт
        # (оценка менее надёжна — снайпер должен быть ещё дешевле)
        median_p = statistics.median(historical_prices)
        ema_val = _ema_fair_value(historical_prices)

        # Консервативная справедливая цена: min(медиана, EMA)
        fair_value = min(median_p, ema_val)
        est_sell = fair_value * 0.95

        # Жёстче: 20% обычно, 40% при стагнирующем рынке
        required_margin = max(config.MIN_PROFIT_MARGIN, 0.40 if stale_market else 0.20)
        if est_sell < current_price * (1 + required_margin):
            logger.debug(
                f"Sparse sniper for {item_id}: est_sell={est_sell:,.0f} < "
                f"buy={current_price:,} × {1+required_margin:.2f} "
                f"({'stale market' if stale_market else 'sparse data'}) — skipping."
            )
            return (False, 0.0, 0.0, False)

        logger.info(
            f"SNIPER (sparse {len(historical_prices)} pts{'  ⚠️ stale' if stale_market else ''}) for {item_id}: "
            f"Price={current_price:,} | fair_value={fair_value:,.0f} | est_sell={est_sell:,.0f}"
        )
        return (True, est_sell, 0.0, True)

    # ── 6. Полный анализ (15+ точек) ────────────────────────────────────────
    prices = list(historical_prices) + [current_price]

    try:
        df = pd.DataFrame(prices, columns=['close'])

        # Полосы Боллинджера
        bb = BollingerBands(close=df['close'], window=10, window_dev=2)
        lower_band   = bb.bollinger_lband().iloc[-1]
        middle_band  = bb.bollinger_mavg().iloc[-1]
        std_dev      = df['close'].rolling(window=10).std().iloc[-1]

        # RSI (индекс относительной силы)
        rsi_val = RSIIndicator(close=df['close'], window=10).rsi().iloc[-1]

        # MACD (схождение/расхождение скользящих средних)
        macd_ind    = MACD(close=df['close'], window_fast=6, window_slow=13, window_sign=4)
        macd_val    = macd_ind.macd().iloc[-1]
        macd_signal = macd_ind.macd_signal().iloc[-1]

        # Z-оценка отклонения цены от средней
        z_score = (current_price - middle_band) / std_dev if std_dev > 0 else 0.0

        # Проверка на NaN/Inf
        for v in (lower_band, middle_band, z_score, rsi_val):
            if pd.isna(v) or math.isinf(v) or math.isnan(v):
                return (False, 0.0, 0.0, False)

        # ── 7. Целевая цена продажи ──────────────────────────────────────────
        # EMA-справедливая цена (свежие цены весят больше)
        ema_val = _ema_fair_value(prices)

        # Консервативно: берём min(BB-middle, EMA).
        # При падении цен EMA уже это отразит.
        bb_sell  = float(middle_band * 0.95)
        ema_sell = float(ema_val     * 0.95)
        estimated_sell_price = min(bb_sell, ema_sell)

        # Штраф за стагнацию: история из одной цены (CV < 5%) — это листинг,
        # который никто не купил, а не реальная сделка. Режем оценку на 50%,
        # чтобы отразить неопределённость рыночной цены.
        if stale_market:
            estimated_sell_price *= 0.50
            logger.debug(
                f"[{item_id}] Штраф за стагнацию: est_sell уменьшен до {estimated_sell_price:,.0f}"
            )

        # ── 9. Умный андеркат по живым конкурирующим ценам ─────────────────
        if competing_prices and len(competing_prices) >= 2:
            min_comp = min(competing_prices)
            max_comp = max(competing_prices)
            # Спред >5× — нет консенсуса по цене.
            # Возможно: 1М (реальный) + 10М (тролль/хранилище) — цель продажи ненадёжна.
            if min_comp > 0 and (max_comp / min_comp) > 5.0:
                logger.debug(
                    f"[{item_id}] Huge price spread detected: {min_comp:,.0f} – {max_comp:,.0f} "
                    f"(ratio={max_comp/min_comp:.1f}x). Skipping — no market consensus."
                )
                return (False, 0.0, 0.0, False)

        if competing_prices:
            # Используем конкурирующую цену как ориентир, только если она адекватна
            # (не тролль-цена далеко выше нашей оценки справедливой стоимости).
            sane_competing = [p for p in competing_prices if p <= estimated_sell_price * 3.0]
            if sane_competing:
                smart_sell = min(sane_competing) * 0.99
                if smart_sell > current_price:
                    estimated_sell_price = min(estimated_sell_price, smart_sell)

        # ── 10. Минимальная маржа прибыли (без исключений, стагнация строже) ─
        # Стагнирующий рынок = почти одинаковые исторические цены (один листинг).
        # Не доверяем таким ценам — требуем выше маржу.
        required_margin_full = config.MIN_PROFIT_MARGIN * (2.5 if stale_market else 1.0)
        min_required_sell = current_price * (1.0 + required_margin_full)
        if estimated_sell_price < min_required_sell:
            logger.debug(
                f"Skipping {item_id}: est_sell={estimated_sell_price:,.0f} < "
                f"buy={current_price:,} + {required_margin_full*100:.0f}% = {min_required_sell:,.0f}. "
                f"Stale={stale_market} Sniper={sniper}"
            )
            return (False, 0.0, 0.0, False)

        # ── Консенсусные сигналы покупки ──────────────────────────────────────
        z_reversion      = (z_score < -2.0)
        bb_rsi_reversion = (current_price < lower_band) and (rsi_val < 45)
        macd_reversion   = (z_score < -1.5) and (macd_val > macd_signal) and (rsi_val < 40)
        buy_signal = z_reversion or bb_rsi_reversion or macd_reversion

        if buy_signal:
            logger.info(
                f"Consensus BUY for {item_id}: "
                f"Z={z_score:.2f} | RSI={rsi_val:.1f} | "
                f"Price={current_price:,} | est_sell={estimated_sell_price:,.0f}"
            )

        # Снайперский override — маржа уже проверена выше
        if sniper and not buy_signal:
            buy_signal = True
            logger.info(
                f"SNIPER BUY for {item_id}: "
                f"Price={current_price:,} < {config.SNIPER_THRESHOLD*100:.0f}% of "
                f"median={statistics.median(historical_prices):,.0f} | "
                f"est_sell={estimated_sell_price:,.0f} "
                f"(+{(estimated_sell_price/current_price - 1)*100:.1f}%)"
            )

        return (bool(buy_signal), estimated_sell_price, float(z_score), sniper)

    except Exception as e:
        logger.error(f"Error in should_buy for '{item_id}': {e}", exc_info=True)
        return (False, 0.0, 0.0, False)
