import asyncio
import json
import logging
import sys
import time
import datetime
from websockets.server import serve
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError
from colorama import init, Fore, Style


# Поддержка Unicode/кириллицы в консоли Windows
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# Цветной вывод в консоль
init(autoreset=True)

# Форматтер логов с цветом и временем
class CustomFormatter(logging.Formatter):
    def format(self, record):
        log_fmt = f"{Style.DIM}%(asctime)s{Style.RESET_ALL} [%(levelname)s] %(message)s"
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)

# Настройка логгирования
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(CustomFormatter())
root_logger.addHandler(handler)

logger = logging.getLogger("WS-Server")

import os
from dotenv import load_dotenv
import config
import database
import analyzer
import telegram_bot

logger.info(f"Initialized server in TRADING_MODE: {config.TRADING_MODE}")

# Таймауты
CONFIRM_TIMEOUT_S = 8.0    # ожидание GUI подтверждения покупки
DELIVERY_TIMEOUT_S = 30.0  # ожидание доставки предмета в инвентарь


async def _open_ah_on_clients():
    """Sends OPEN_AH to all connected Minecraft clients."""
    command = json.dumps({"type": "ACTION_COMMAND", "payload": {"action": "OPEN_AH"}})
    for ws in list(config.CONNECTED_CLIENTS):
        try:
            await ws.send(command)
        except Exception as e:
            logger.warning(f"{Fore.YELLOW}Failed to send OPEN_AH: {e}")


async def _schedule_next_scan(delay_seconds: int):
    """
    Waits `delay_seconds` then sends OPEN_AH to all clients.
    Stores itself in config.PENDING_SCAN_TASK so it can be cancelled
    (e.g. when the user changes the interval via Telegram).
    """
    if delay_seconds > 0:
        mins, secs = divmod(delay_seconds, 60)
        logger.info(
            f"{Fore.CYAN}⏱  Timed mode — next scan in "
            f"{f'{mins}м {secs}с' if mins else f'{secs}с'} "
            f"({delay_seconds}s)."
        )
        await asyncio.sleep(delay_seconds)
    logger.info(f"{Fore.GREEN}▶  Triggering next market scan (OPEN_AH).")
    await _open_ah_on_clients()
    config.PENDING_SCAN_TASK = None


def _get_navigate_direction(current_page, total_pages) -> str:
    """Returns NEXT, PREV, or REFRESH depending on the current scan position."""
    # Двунаправленная навигация (аналог scheduleNextNavigation из Java-клиента)
    # Направление хранится в config, чтобы не сбрасываться между страницами
    if not hasattr(config, 'SCAN_DIRECTION'):
        config.SCAN_DIRECTION = 'NEXT'  # вперёд по умолчанию
    if not hasattr(config, 'HAS_PARSED_PAGES'):
        config.HAS_PARSED_PAGES = False

    config.HAS_PARSED_PAGES = True

    if config.SCAN_DIRECTION == 'NEXT' and current_page >= total_pages and total_pages > 1:
        # Дошли до последней страницы — разворачиваемся
        config.SCAN_DIRECTION = 'PREV'
        return 'REFRESH'
    if config.SCAN_DIRECTION == 'PREV' and current_page <= 1:
        # Дошли до первой страницы — разворачиваемся
        config.SCAN_DIRECTION = 'NEXT'
        return 'REFRESH'

    return config.SCAN_DIRECTION


async def handle_connection(websocket):
    client_address = websocket.remote_address
    logger.info(f"{Fore.GREEN}Client connected from {client_address[0]}:{client_address[1]}")
    
    # Регистрируем клиента для отправки команд
    config.CONNECTED_CLIENTS.add(websocket)
    
    # Машина состояний: SCANNING -> WAITING_CONFIRM -> WAITING_DELIVERY -> SCANNING
    state = "SCANNING"
    state_data = {}
    
    try:
        async for message in websocket:
            now = asyncio.get_event_loop().time()

            # --- Проверки таймаутов ---
            if state == "WAITING_CONFIRM":
                elapsed = now - state_data.get("timestamp", now)
                if elapsed > CONFIRM_TIMEOUT_S:
                    item_name = state_data.get('expected_item', '?')
                    deal_id   = state_data.get('deal_id')
                    logger.warning(
                        f"{Fore.YELLOW}[TIMEOUT] WAITING_CONFIRM timed out after {elapsed:.1f}s "
                        f"(item: {item_name}). Marking deal FAILED and resetting to SCANNING."
                    )
                    # Подтверждение так и не пришло — отмечаем сделку как FAILED
                    if deal_id is not None:
                        await database.update_deal_status(deal_id, "FAILED")
                    state = "SCANNING"
                    state_data = {}

            elif state == "WAITING_DELIVERY":
                elapsed = now - state_data.get("timestamp", now)
                if elapsed > DELIVERY_TIMEOUT_S:
                    logger.warning(
                        f"{Fore.YELLOW}[TIMEOUT] WAITING_DELIVERY timed out after {elapsed:.1f}s. "
                        f"Resetting to SCANNING."
                    )
                    state = "SCANNING"
                    state_data = {}

            try:
                # Разбираем входящее сообщение
                if isinstance(message, bytes):
                    message_str = message.decode('utf-8')
                else:
                    message_str = message
                
                data = json.loads(message_str)
                msg_type = data.get("type", "UNKNOWN")
                
                # ─── CONFIRM_GUI ───────────────────────────────────────────────
                if msg_type == "CONFIRM_GUI":
                    payload = data.get("payload", {})
                    confirm_slot = payload.get("confirm_slot", -1)
                    confirm_item = payload.get("expected_item", "")
                    
                    if state == "WAITING_CONFIRM":
                        logger.info(
                            f"{Fore.MAGENTA}Received CONFIRM_GUI. Confirming purchase of "
                            f"{state_data['expected_item']} (slot: {confirm_slot})."
                        )

                        confirm_command = {
                            "type": "ACTION_COMMAND",
                            "payload": {
                                "action": "CLICK_SLOT",
                                "slot": confirm_slot,
                                "expected_item": confirm_item,
                                "delay_ms": 150
                            }
                        }
                        await websocket.send(json.dumps(confirm_command))

                        # Сохраняем сделку только после отправки клика на подтверждение.
                        # Раньше не сохраняем — иначе появятся фантомные записи BOUGHT.
                        saved_deal_id = await database.save_deal(
                            state_data["expected_item"],
                            state_data["price"],
                            state_data["target_sell_price"],
                        )
                        state_data["deal_id"] = saved_deal_id
                        logger.info(f"{Fore.GREEN}Deal #{saved_deal_id} saved to DB.")

                        # Уведомление в Telegram (не блокируем event loop)
                        alert_key = (state_data["expected_item"], state_data["price"])
                        cooldown = 60 if state_data.get("is_sniper") else 300
                        if time.time() - config.RECENT_ALERTS.get(alert_key, 0) >= cooldown:
                            config.RECENT_ALERTS[alert_key] = time.time()
                            asyncio.create_task(
                                telegram_bot.send_deal_notification(
                                    state_data["display_name"],
                                    state_data["price"],
                                    state_data["target_sell_price"],
                                    state_data["z_score"],
                                    item_id=state_data["expected_item"],
                                    historical_prices=state_data.get("historical_prices", []),
                                    is_sniper=state_data.get("is_sniper", False),
                                    mode="ACTIVE",
                                    expires_in_seconds=state_data.get("expires_in_seconds", -1),
                                    listing_staleness=state_data.get("listing_staleness", 0),
                                )
                            )

                        # Переход: WAITING_CONFIRM -> WAITING_DELIVERY
                        state = "WAITING_DELIVERY"
                        state_data["timestamp"] = asyncio.get_event_loop().time()
                        logger.info(f"{Fore.MAGENTA}State -> WAITING_DELIVERY.")

                        # Перепродажа через 2с — предмет должен появиться в инвентаре
                        item_id_to_sell = state_data["expected_item"]
                        sell_price = state_data["target_sell_price"]
                        deal_id = state_data.get("deal_id")
                        
                        async def schedule_resell(_item_id, _sell_price, _deal_id):
                            await asyncio.sleep(2.0)
                            logger.info(
                                f"{Fore.GREEN}Sending SELL_ITEM command for {_item_id} "
                                f"at price {_sell_price:,}$."
                            )
                            sell_command = {
                                "type": "ACTION_COMMAND",
                                "payload": {
                                    "action": "SELL_ITEM",
                                    "item_id": _item_id,
                                    "price": int(_sell_price)
                                }
                            }
                            try:
                                await websocket.send(json.dumps(sell_command))
                                # Обновляем статус сделки в БД
                                if _deal_id is not None:
                                    await database.update_deal_status(_deal_id, "SELL_PENDING", _sell_price)
                            except Exception as ex:
                                logger.error(f"Error sending SELL_ITEM: {ex}")
                            
                            nonlocal state, state_data
                            state = "SCANNING"
                            state_data = {}
                            logger.info(f"{Fore.CYAN}State -> SCANNING (after resell command).")
                            
                        asyncio.create_task(schedule_resell(item_id_to_sell, sell_price, deal_id))
                    else:
                        logger.warning(f"{Fore.YELLOW}Unexpected CONFIRM_GUI in state {state}. Ignoring.")

                # ─── AUCTION_DATA ──────────────────────────────────────────────
                elif msg_type == "AUCTION_DATA":
                    if state != "SCANNING":
                        # Не обрабатываем страницы аукциона во время покупки
                        continue

                    payload = data.get("payload", {})
                    items = payload.get("items", [])
                    page = payload.get("current_page", "?")
                    total = payload.get("total_pages", "?")
                    balance = payload.get("balance", -1)
                    if balance >= 0:
                        config.CURRENT_BALANCE = balance
                    
                    balance_str = f" | Balance: {balance:,}$" if balance >= 0 else ""
                    logger.info(f"{Fore.CYAN}AUCTION_DATA [Page {page}/{total}]{balance_str} — {len(items)} items")
                    
                    for item in items:
                        display_name = item.get("display_name", "Unknown Item")
                        price = item.get("price", 0)
                        seller = item.get("seller", "Unknown")
                        item_id = item.get("item_id", "")
                        slot = item.get("slot", "?")
                        
                        logger.info(
                            f"  {Fore.YELLOW}Slot {slot}: {Style.BRIGHT}{display_name}{Style.RESET_ALL} "
                            f"({item_id}) | {Fore.GREEN}{price:,}${Style.RESET_ALL} | {Fore.BLUE}{seller}"
                        )
                        
                        if price <= 0:
                            continue

                        # Записываем активность продавца
                        await database.save_seller_activity(seller, item_id, price)

                        # Отпечаток листинга (seller + item_id + price).
                        # times_seen = сколько циклов сканирования листинг не менялся.
                        listing_staleness = await database.upsert_listing_fingerprint(seller, item_id, price)

                        # Оставшееся время листинга (секунды) — лучший индикатор свежести.
                        # Малое значение = листинг висит давно.
                        # -1 = поле недоступно (старый клиент или предмет без срока).
                        expires_in_seconds = item.get("expires_in_seconds", -1)

                        # История цен предмета
                        historical_prices = await database.get_recent_prices(item_id, limit=30)

                        # Сохраняем текущую цену
                        await database.save_price(item_id, display_name, price)

                        # Пропускаем неликвидные предметы (< 3 наблюдений)
                        liquidity = await database.get_item_liquidity(item_id)
                        if liquidity < 3 and len(historical_prices) >= 3:
                            continue

                        # Конкурирующие цены для умного андерката
                        competing_prices = await database.get_competing_prices(item_id, limit=10)


                        should_buy, estimated_sell, z_score, is_sniper = analyzer.should_buy(
                            item_id, price, historical_prices,
                            competing_prices=competing_prices,
                            listing_staleness=listing_staleness,
                            expires_in_seconds=expires_in_seconds,
                        )


                        if not should_buy:
                            continue

                        deal_prefix = "🎯 SNIPER" if is_sniper else "📊 CONSENSUS"
                        has_balance = (balance < 0 or price <= balance)

                        # ── Лимит слотов продажи ──────────────────────────────────
                        MAX_SELL_SLOTS = 15
                        active_deals = await database.get_active_deals_count()
                        if active_deals >= MAX_SELL_SLOTS:
                            logger.info(
                                f"  {Fore.YELLOW}[SLOT CAP] {display_name} @ {price:,}$ skipped "
                                f"— sell slots full ({active_deals}/{MAX_SELL_SLOTS})."
                            )
                            continue

                        if config.TRADING_MODE == "ACTIVE" and has_balance:
                            logger.info(
                                f"  {Fore.RED}{Style.BRIGHT}!!! {deal_prefix} BUY SIGNAL !!! "
                                f"Z={z_score:.2f} | {display_name} @ {price:,}$ -> sell ~{estimated_sell:,.0f}$ "
                                f"[слот {active_deals+1}/{MAX_SELL_SLOTS}]"
                            )
                            
                            buy_command = {
                                "type": "ACTION_COMMAND",
                                "payload": {
                                    "action": "CLICK_SLOT",
                                    "slot": slot,
                                    "expected_item": item_id,
                                    "delay_ms": 50 if is_sniper else 75
                                }
                            }
                            await websocket.send(json.dumps(buy_command))
                            # Сделка НЕ сохраняется в БД здесь — только после получения
                            # CONFIRM_GUI. Иначе при отсутствии подтверждения появятся
                            # фантомные записи BOUGHT.

                            # Переход -> WAITING_CONFIRM (deal_id пока None)
                            state = "WAITING_CONFIRM"
                            state_data = {
                                "expected_item": item_id,
                                "display_name": display_name,
                                "price": price,
                                "target_sell_price": estimated_sell,
                                "z_score": z_score,
                                "is_sniper": is_sniper,
                                "historical_prices": historical_prices,
                                "expires_in_seconds": expires_in_seconds,
                                "listing_staleness": listing_staleness,
                                "deal_id": None,  # заполняется после CONFIRM_GUI
                                "timestamp": asyncio.get_event_loop().time(),
                            }
                            logger.info(f"{Fore.MAGENTA}State -> WAITING_CONFIRM (deal not yet saved).")
                            break  # process one deal at a time

                        elif config.TRADING_MODE == "ACTIVE" and not has_balance:
                            logger.info(
                                f"  {Fore.YELLOW}{deal_prefix} [LOW BALANCE] {display_name} @ {price:,}$ "
                                f"(balance: {balance:,}$). Skipping buy."
                            )
                            alert_key = (item_id, price)
                            if time.time() - config.RECENT_ALERTS.get(alert_key, 0) >= 300:
                                config.RECENT_ALERTS[alert_key] = time.time()
                                asyncio.create_task(
                                    telegram_bot.send_deal_notification(
                                        display_name, price, estimated_sell, z_score,
                                        item_id=item_id,
                                        historical_prices=historical_prices,
                                        is_sniper=is_sniper,
                                        warning=f"⚠️ Недостаточно баланса! Баланс: {balance:,}$",
                                        mode="ACTIVE",
                                        expires_in_seconds=expires_in_seconds,
                                        listing_staleness=listing_staleness,
                                    )
                                )
                        else:
                            # Режим SCANNING — только уведомление
                            logger.info(
                                f"  {Fore.YELLOW}{deal_prefix} [SCANNING] {display_name} @ {price:,}$ "
                                f"(sell ~{estimated_sell:,.0f}$, Z={z_score:.2f})"
                            )
                            # Предупреждение только при реальной проблеме
                            warning_msg = None
                            if balance >= 0 and price > balance:
                                warning_msg = f"⚠️ Недостаточно баланса! Баланс: {balance:,}$"
                            alert_key = (item_id, price)
                            if time.time() - config.RECENT_ALERTS.get(alert_key, 0) >= 300:
                                config.RECENT_ALERTS[alert_key] = time.time()
                                asyncio.create_task(
                                    telegram_bot.send_deal_notification(
                                        display_name, price, estimated_sell, z_score,
                                        item_id=item_id,
                                        historical_prices=historical_prices,
                                        is_sniper=is_sniper,
                                        warning=warning_msg,
                                        mode="SCANNING",
                                        expires_in_seconds=expires_in_seconds,
                                        listing_staleness=listing_staleness,
                                    )
                                )

                # ── Команда навигации на Java-клиент ────────────────────────
                # Отправляем только в SCANNING — при покупке (break) состояние уже другое.
                if state == "SCANNING":
                    nav_direction = _get_navigate_direction(page, total)
                    nav_cmd = {
                        "type": "ACTION_COMMAND",
                        "payload": {"action": "NAVIGATE", "direction": nav_direction}
                    }
                    await websocket.send(json.dumps(nav_cmd))

                # ─── MARKET_REFRESH ────────────────────────────────────────────
                elif msg_type == "MARKET_REFRESH":
                    cycle = data.get("payload", {}).get("cycle", "?")
                    interval = config.SCAN_INTERVAL_SECONDS
                    mode_label = "НЕПРЕРЫВНЫЙ" if interval == 0 else f"ТАЙМЕР {interval//60}м {interval%60}с"
                    logger.info(
                        f"{Fore.CYAN}{'═'*55}\n"
                        f"  🔄 MARKET REFRESH — Scan cycle #{cycle} complete.\n"
                        f"  Режим: {mode_label}\n"
                        f"{'═'*55}"
                    )
                    # Принудительный сброс машины состояний при обновлении рынка
                    if state != "SCANNING":
                        logger.warning(
                            f"{Fore.YELLOW}State was '{state}' during market refresh — "
                            f"force-resetting to SCANNING."
                        )
                        state = "SCANNING"
                        state_data = {}

                    # Отменяем предыдущий таймер сканирования
                    if config.PENDING_SCAN_TASK and not config.PENDING_SCAN_TASK.done():
                        config.PENDING_SCAN_TASK.cancel()

                    # Планируем следующий скан: немедленно (0с) или с задержкой
                    if config.SCANNER_ENABLED:
                        config.PENDING_SCAN_TASK = asyncio.create_task(
                            _schedule_next_scan(interval)
                        )

                # ─── Other message types ───────────────────────────────────────
                elif msg_type == "ACTION_COMMAND":
                    logger.info(f"{Fore.MAGENTA}Received ACTION_COMMAND: {json.dumps(data, indent=2, ensure_ascii=False)}")
                else:
                    logger.info(f"{Fore.WHITE}Received {msg_type}: {json.dumps(data, indent=2, ensure_ascii=False)}")

                    
            except json.JSONDecodeError:
                preview = message[:200] if isinstance(message, str) else message[:200].decode('utf-8', errors='ignore')
                logger.warning(f"{Fore.RED}Non-JSON message: {preview}")
            except Exception as e:
                logger.error(f"{Fore.RED}Error processing message: {e}", exc_info=True)
                
    except ConnectionClosedOK:
        logger.info(f"{Fore.YELLOW}Client disconnected gracefully: {client_address[0]}:{client_address[1]}")
    except ConnectionClosedError as e:
        logger.warning(f"{Fore.RED}Client disconnected with error (code={e.code}): {client_address[0]}:{client_address[1]}")
    except Exception as e:
        logger.error(f"{Fore.RED}Unexpected connection error: {e}", exc_info=True)
    finally:
        config.CONNECTED_CLIENTS.discard(websocket)
        logger.info(f"{Fore.YELLOW}Connection closed: {client_address[0]}:{client_address[1]}")


async def daily_cleanup_task():
    """Runs database cleanup every 24 hours."""
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            logger.info(f"{Fore.CYAN}Running daily database cleanup (retention: {config.DB_RETENTION_DAYS} days)...")
            await database.cleanup_old_data(config.DB_RETENTION_DAYS)
            logger.info(f"{Fore.GREEN}Daily database cleanup completed.")
        except Exception as e:
            logger.error(f"{Fore.RED}Error during daily cleanup: {e}", exc_info=True)


async def daily_report_task():
    """Sends a daily Telegram report at the configured hour."""
    while True:
        now = datetime.datetime.now()
        target = now.replace(hour=config.DAILY_REPORT_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(f"{Fore.CYAN}Daily report scheduled in {wait_seconds/3600:.1f} hours.")
        await asyncio.sleep(wait_seconds)
        try:
            logger.info(f"{Fore.CYAN}Generating daily Telegram report...")
            await telegram_bot.send_daily_report()
            logger.info(f"{Fore.GREEN}Daily report sent successfully.")
        except Exception as e:
            logger.error(f"{Fore.RED}Error sending daily report: {e}", exc_info=True)


async def main():
    await database.init_db()
    
    # Запуск polling бота в фоне
    if telegram_bot.TOKEN:
        asyncio.create_task(telegram_bot.start_polling_bot())
    
    # Ежедневные задачи
    asyncio.create_task(daily_cleanup_task())
    if telegram_bot.TOKEN:
        asyncio.create_task(daily_report_task())
        
    host = "0.0.0.0"
    port = 8080
    logger.info(f"{Fore.GREEN}Starting WebSocket server on {host}:{port}...")
    async with serve(handle_connection, host, port):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info(f"\n{Fore.YELLOW}Server stopped by user.")
