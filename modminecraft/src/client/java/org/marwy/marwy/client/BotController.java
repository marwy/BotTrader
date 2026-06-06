package org.marwy.marwy.client;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gui.screen.ingame.HandledScreen;
import net.minecraft.entity.player.PlayerInventory;
import net.minecraft.item.ItemStack;
import net.minecraft.nbt.NbtCompound;
import net.minecraft.nbt.NbtList;
import net.minecraft.registry.Registries;
import net.minecraft.screen.GenericContainerScreenHandler;
import net.minecraft.screen.ScreenHandler;
import net.minecraft.screen.slot.Slot;
import net.minecraft.screen.slot.SlotActionType;
import net.minecraft.text.Style;
import net.minecraft.text.Text;
import net.minecraft.text.TextColor;
import net.minecraft.util.Formatting;
import net.minecraft.scoreboard.Scoreboard;
import net.minecraft.scoreboard.ScoreboardObjective;
import net.minecraft.scoreboard.ScoreboardPlayerScore;
import net.minecraft.scoreboard.Team;

import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class BotController {
    private static final BotController INSTANCE = new BotController();
    private static final Gson GSON = new Gson();

    private final ScheduledExecutorService clickScheduler = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread thread = new Thread(r, "Marwy-Click-Scheduler");
        thread.setDaemon(true);
        return thread;
    });

    private boolean isScannerEnabled = true;
    private int ticksSinceLastScan = 0;
    private static final int SCAN_INTERVAL_TICKS = 3600; // 3 минуты
    private int ticksSinceLastPageUpdate = 0;
    private static final int MAX_STUCK_TICKS = 160; // 8 секунд — таймаут зависания

    /** true пока ждём GUI подтверждения покупки.
     *  Подавляет авто-навигацию, давая Python время ответить. */
    private boolean isBuying = false;

    private String lastSentSignature = "";
    private ScheduledFuture<?> pendingNavigationTask = null;
    private int currentPage = 1;
    private int totalPages = 1;
    private boolean isBotInitiatedScan = false;
    private boolean hasParsedPages = false;
    private int lastScheduledNavigationPage = -1;
    private boolean isNavigatingForward = true;
    private int scanCycleCount = 0;

    private BotController() {}

    public static BotController getInstance() {
        return INSTANCE;
    }

    private boolean isOnGameServer() {
        MinecraftClient client = MinecraftClient.getInstance();
        if (client.world == null || client.player == null) return false;
        
        // Если баланс читается — точно на игровом сервере
        if (getPlayerBalance() >= 0) return true;
        
        // Запасная проверка по скорборду
        Scoreboard scoreboard = client.world.getScoreboard();
        if (scoreboard != null) {
            for (ScoreboardObjective obj : scoreboard.getObjectives()) {
                String title = obj.getDisplayName().getString().toLowerCase();
                if (title.contains("аукцион") || title.contains("рынок") || title.contains("гриф") || title.contains("анархия") || title.contains("survival") || title.contains("play")) {
                    return true;
                }
            }
        }
        return false;
    }

    public void onClientTick(MinecraftClient client) {
        if (client.player == null || client.world == null) {
            return;
        }

        // 1. Периодический авто-скан
        if (isScannerEnabled) {
            ticksSinceLastScan++;
            if (ticksSinceLastScan >= SCAN_INTERVAL_TICKS) {
                ticksSinceLastScan = 0;
                if (client.currentScreen == null && isOnGameServer()) {
                    System.out.println("[MarwyBot] Periodic scan: sending /ah command.");
                    isBotInitiatedScan = true;
                    client.player.networkHandler.sendCommand("ah");
                }
            }
        }

        // 2. Обработка текущего экрана — аукцион или GUI подтверждения
        if (client.currentScreen instanceof HandledScreen<?> handledScreen) {
            Text titleText = handledScreen.getTitle();
            String title = titleText != null ? titleText.getString().toLowerCase() : "";
            
            // Проверяем что это сундук-GUI
            if (handledScreen.getScreenHandler() instanceof GenericContainerScreenHandler containerHandler) {
                boolean isAuction = title.contains("auction") || title.contains("аукцион") || title.contains("ah") || title.contains("рынок");

                if (isAuction) {
                    ticksSinceLastPageUpdate++;
                    if (ticksSinceLastPageUpdate >= MAX_STUCK_TICKS) {
                        ticksSinceLastPageUpdate = 0;

                        if (isBuying) {
                            // Не навигируем пока ждём подтверждение покупки — Python может ещё обрабатывать.
                            System.out.println("[MarwyBot] Stuck timer fired but isBuying=true — skipping nav.");
                            // Сбрасываем, чтобы следующий таймаут навигировал нормально.
                            isBuying = false;
                        } else {
                            int[] pageInfo = parsePageInfo(titleText != null ? titleText.getString() : "");
                            int current = pageInfo[0];
                            int total   = pageInfo[1];

                            // Застряли на последней странице → перезапускаем рынок
                            if (total > 1 && current >= total) {
                                System.out.println("[MarwyBot] Stuck at last page " + current + "/" + total
                                        + " — triggering market refresh.");
                                scheduleMarketRefresh(containerHandler.syncId);
                            } else {
                                // Застряли в середине → повтор текущего направления
                                int targetSlot = isNavigatingForward ? 50 : 48;
                                System.out.println("[MarwyBot] Page scan stuck! Page " + current + "/" + total
                                        + ". Retrying slot " + targetSlot + ".");
                                clickSlot(containerHandler.syncId, targetSlot, null);
                            }
                        }
                    }
                    parseAndSendAuctionData(containerHandler, titleText != null ? titleText.getString() : "Auction");
                } else {
                    // Не аукцион — возможно GUI подтверждения покупки.
                    // Всегда ищем кнопку подтверждения (lime glass pane).
                    ticksSinceLastPageUpdate = 0;
                    cancelPendingNavigation();
                    detectAndSendConfirmation(containerHandler, titleText != null ? titleText.getString() : "Confirm");
                }
            } else {
                ticksSinceLastPageUpdate = 0;
                cancelPendingNavigation();
            }
        } else {
            // Экран закрыт — покупка завершена/отменена или AH закрыт.
            lastSentSignature = "";
            ticksSinceLastPageUpdate = 0;
            isBotInitiatedScan = false;
            isBuying = false;
            lastScheduledNavigationPage = -1;
            isNavigatingForward = true;
            cancelPendingNavigation();
        }
    }

    private void parseAndSendAuctionData(GenericContainerScreenHandler handler, String screenTitle) {
        MinecraftClient client = MinecraftClient.getInstance();
        if (client.player == null) return;

        JsonArray itemsArray = new JsonArray();
        StringBuilder signatureBuilder = new StringBuilder();

        int[] pageInfo = parsePageInfo(screenTitle);
        int current = pageInfo[0];
        int total = pageInfo[1];
        this.currentPage = current;
        this.totalPages = total;
        
        // Управление направлением — только в scheduleNextNavigation.
        // Переключение isNavigatingForward здесь сломает проверки границ → refresh не сработает.

        for (Slot slot : handler.slots) {
            // Пропускаем слоты инвентаря игрока
            if (slot.inventory instanceof PlayerInventory) {
                continue;
            }

            ItemStack stack = slot.getStack();
            if (stack.isEmpty()) {
                continue;
            }

            String baseItemId = Registries.ITEM.getId(stack.getItem()).toString();
            // Пропускаем заглушки GUI (стеклянные панели)
            if (baseItemId.endsWith("stained_glass_pane")) {
                continue;
            }

            String displayName = toLegacyText(stack.getName());
            List<String> lore = extractLore(stack);
            int price = parsePrice(lore);
            String itemId = getUniqueItemId(stack, baseItemId, displayName, lore);

            JsonObject itemJson = new JsonObject();
            itemJson.addProperty("slot", slot.id);
            itemJson.addProperty("item_id", itemId);
            itemJson.addProperty("display_name", displayName);
            itemJson.addProperty("price", price);
            itemJson.addProperty("seller", extractSeller(lore));

            // Остаток времени листинга: малое значение = старый лот = устаревший сигнал рынка.
            long expirySeconds = parseExpirySeconds(lore);
            if (expirySeconds >= 0) {
                itemJson.addProperty("expires_in_seconds", expirySeconds);
            }

            JsonArray loreArray = new JsonArray();
            for (String line : lore) {
                loreArray.add(line);
            }
            itemJson.add("lore", loreArray);
            itemsArray.add(itemJson);

            // Сигнатура для обнаружения изменений страницы
            signatureBuilder.append(slot.id).append(":").append(itemId).append(":").append(price).append(";");
        }

        String currentSignature = signatureBuilder.toString();
        if (currentSignature.equals(lastSentSignature) || itemsArray.size() == 0) {
            return; // Нет изменений или пустой GUI
        }
        lastSentSignature = currentSignature;
        ticksSinceLastPageUpdate = 0; // Сброс детектора зависания — пришли новые данные

        // Читаем баланс со скорборда
        long balance = getPlayerBalance();

        // Формируем пакет AUCTION_DATA
        JsonObject payload = new JsonObject();
        payload.addProperty("current_page", current);
        payload.addProperty("total_pages", total);
        payload.addProperty("balance", balance);
        payload.add("items", itemsArray);

        JsonObject packet = new JsonObject();
        packet.addProperty("type", "AUCTION_DATA");
        packet.add("payload", payload);

        System.out.println("[MarwyBot] Sending AUCTION_DATA packet for Page " + current + "/" + total
                + " (Balance: " + balance + ") with " + itemsArray.size() + " items.");
        WebsocketClientConnector.getInstance().send(GSON.toJson(packet));

        // Навигация полностью управляется Python'ом.
        // Python отвечает на каждый AUCTION_DATA одной из команд:
        //   ACTION_COMMAND { action: "NAVIGATE", direction: "NEXT" / "PREV" / "REFRESH" }
        //   ACTION_COMMAND { action: "CLICK_SLOT", slot: N, expected_item: "...", delay_ms: N }
        // Это исключает гонку, когда навигация срабатывала до ответа Python'а.
        // Детектор зависания в onTick() — запасной вариант при молчании Python'а.
    }

    private void scheduleNextNavigation(GenericContainerScreenHandler handler, int current, int total) {
        // Не перепланируем навигацию для уже обработанной страницы
        if (current == lastScheduledNavigationPage) {
            return;
        }
        lastScheduledNavigationPage = current;

        cancelPendingNavigation();

        MinecraftClient client = MinecraftClient.getInstance();
        if (client.player == null) return;

        // ── Проверка границ сканирования ─────────────────────────────────────
        // Последняя страница (конец прямого прохода) или страница 1 (конец обратного):
        // обновляем рынок вместо смены направления.
        // Закрытие и повторное открытие AH убирает проданные лоты и показывает новые.
        boolean atLastPage  = isNavigatingForward  && hasParsedPages && current >= total && total > 1;
        boolean atFirstPage = !isNavigatingForward && current <= 1;

        if (atLastPage) {
            System.out.println("[MarwyBot] ═══ Reached last page (" + current + "/" + total
                    + "). Market refresh → will scan backward from fresh list. ═══");
            isNavigatingForward = false;
            scheduleMarketRefresh(handler.syncId);
            return;
        }

        if (atFirstPage) {
            System.out.println("[MarwyBot] ═══ Reached page 1 (full cycle done). Market refresh → starting new cycle. ═══");
            isNavigatingForward = true;
            scheduleMarketRefresh(handler.syncId);
            return;
        }

        // ── Обычная навигация по страницам ────────────────────────────────────
        final boolean goForward   = isNavigatingForward;
        final int     targetSlot  = goForward ? 50 : 48;
        final int     syncId      = handler.syncId;

        // Случайная задержка 800–1400мс, с 10% вероятностью — долгая пауза (имитация человека)
        long delayMs = 800 + (long) (Math.random() * 600);
        if (Math.random() < 0.10) {
            delayMs = 3000 + (long) (Math.random() * 2000);
            System.out.println("[MarwyBot] Human simulation: micro-pause of " + delayMs + "ms.");
        }
        final long finalDelayMs = delayMs;

        System.out.println("[MarwyBot] Scheduling " + (goForward ? "FORWARD (slot 50)" : "BACKWARD (slot 48)")
                + " in " + finalDelayMs + "ms. [Page " + current + "/" + total + "]");

        pendingNavigationTask = clickScheduler.schedule(() -> {
            client.execute(() -> {
                if (client.player == null || !(client.currentScreen instanceof HandledScreen<?> handledScreen)) {
                    return;
                }
                ScreenHandler currentHandler = handledScreen.getScreenHandler();
                // Отменяем если с момента планирования открылся другой GUI
                if (currentHandler.syncId != syncId) {
                    System.out.println("[MarwyBot] Navigation aborted: GUI changed (syncId mismatch).");
                    return;
                }
                System.out.println("[MarwyBot] Clicking slot " + targetSlot
                        + " (" + (goForward ? "next" : "prev") + " page).");
                clickSlot(currentHandler.syncId, targetSlot, null);
            });
        }, finalDelayMs, TimeUnit.MILLISECONDS);
    }

    /**
     * Закрывает GUI аукциона и переоткрывает его через небольшую задержку,
     * чтобы получить актуальный список лотов (без проданных, с новыми).
     *
     * @param currentSyncId syncId закрываемого GUI — защита от закрытия другого экрана.
     */
    private void scheduleMarketRefresh(int currentSyncId) {
        cancelPendingNavigation();
        lastScheduledNavigationPage = -1;
        scanCycleCount++;

        // После обновления всегда начинаем с прямого прохода.
        isNavigatingForward = true;

        MinecraftClient client = MinecraftClient.getInstance();
        final int cycleSnapshot = scanCycleCount;
        long closeDelay = 600 + (long) (Math.random() * 400); // 0.6–1.0 с

        System.out.println("[MarwyBot] Cycle #" + cycleSnapshot
                + " complete — closing AH, notifying server.");

        // Шаг 1: закрываем AH
        pendingNavigationTask = clickScheduler.schedule(() -> {
            client.execute(() -> {
                if (client.player == null) return;
                System.out.println("[MarwyBot] [1/2] Closing AH screen.");
                client.player.closeHandledScreen();

                // Шаг 2: уведомляем Python после закрытия экрана (300–500мс)
                // Python сам решит когда прислать OPEN_AH обратно.
                long notifyDelay = 300 + (long) (Math.random() * 200);
                clickScheduler.schedule(() -> {
                    System.out.println("[MarwyBot] [2/2] Sending MARKET_REFRESH to server (cycle #"
                            + cycleSnapshot + ").");
                    sendRefreshNotification();
                    // /ah не вызываем — тайминг сканирования контролирует Python.
                }, notifyDelay, TimeUnit.MILLISECONDS);
            });
        }, closeDelay, TimeUnit.MILLISECONDS);
    }

    /** Отправляет событие MARKET_REFRESH на Python-сервер через WebSocket. */
    private void sendRefreshNotification() {
        try {
            String msg = "{\"type\":\"MARKET_REFRESH\",\"payload\":{\"cycle\":" + scanCycleCount + "}}";
            WebsocketClientConnector.getInstance().send(msg);
        } catch (Exception e) {
            System.err.println("[MarwyBot] Failed to send MARKET_REFRESH: " + e.getMessage());
        }
    }


    private void clickSlot(int syncId, int slotId, String expectedItemId) {
        MinecraftClient client = MinecraftClient.getInstance();
        if (client.player == null || !(client.currentScreen instanceof HandledScreen<?> handledScreen)) {
            return;
        }
        ScreenHandler handler = handledScreen.getScreenHandler();
        if (handler.syncId != syncId) return;

        if (slotId < 0 || slotId >= handler.slots.size()) return;

        if (expectedItemId != null) {
            ItemStack stack = handler.getSlot(slotId).getStack();
            String itemId = Registries.ITEM.getId(stack.getItem()).toString();
            if (!itemId.equals(expectedItemId)) {
                System.err.println("[MarwyBot] Click aborted: expected " + expectedItemId + " but found " + itemId);
                return;
            }
        }

        client.interactionManager.clickSlot(
            syncId,
            slotId,
            0,
            SlotActionType.PICKUP,
            client.player
        );
    }

    private boolean slotExists(ScreenHandler handler, int slotId) {
        return handler != null && slotId >= 0 && slotId < handler.slots.size();
    }

    private int[] parsePageInfo(String screenTitle) {
        if (screenTitle == null) {
            this.hasParsedPages = false;
            return new int[]{1, 1};
        }
        Matcher matcher = Pattern.compile("(\\d+)\\s*/\\s*(\\d+)").matcher(screenTitle);
        if (matcher.find()) {
            try {
                int current = Integer.parseInt(matcher.group(1));
                int total = Integer.parseInt(matcher.group(2));
                this.hasParsedPages = true;
                return new int[]{current, total};
            } catch (NumberFormatException e) {
                // ignore
            }
        }
        this.hasParsedPages = false;
        return new int[]{1, 1};
    }

    private long getPlayerBalance() {
        MinecraftClient client = MinecraftClient.getInstance();
        if (client.world == null) return -1;
        
        Scoreboard scoreboard = client.world.getScoreboard();
        if (scoreboard == null) return -1;
        
        ScoreboardObjective objective = null;
        try {
            int slotId = Scoreboard.getDisplaySlotId("sidebar");
            objective = scoreboard.getObjectiveForSlot(slotId);
        } catch (Exception e) {
            // fallback
        }
        if (objective == null) {
            objective = scoreboard.getObjectiveForSlot(1);
        }
        if (objective == null) {
            for (ScoreboardObjective obj : scoreboard.getObjectives()) {
                objective = obj;
                break;
            }
        }
        if (objective == null) return -1;
        
        try {
            java.util.Collection<ScoreboardPlayerScore> scores = scoreboard.getAllPlayerScores(objective);
            for (ScoreboardPlayerScore score : scores) {
                String scoreHolder = score.getPlayerName();
                Team team = scoreboard.getPlayerTeam(scoreHolder);
                String fullLine;
                if (team != null) {
                    fullLine = team.getPrefix().getString() + scoreHolder + team.getSuffix().getString();
                } else {
                    fullLine = scoreHolder;
                }
                String cleanLine = fullLine.replaceAll("§[0-9a-fk-or]", "").replaceAll("(?i)&[0-9a-fk-or]", "").trim();
                long balance = parseBalanceFromLine(cleanLine);
                if (balance >= 0) {
                    return balance;
                }
            }
        } catch (Exception e) {
            System.err.println("[MarwyBot] Error parsing scoreboard balance: " + e.getMessage());
        }
        return -1;
    }

    private long parseBalanceFromLine(String cleanLine) {
        String lower = cleanLine.toLowerCase().trim();
        if (!lower.contains("баланс") && !lower.contains("balance") && !lower.contains("money") && !lower.contains("монет") && !lower.contains("коин")) {
            return -1;
        }
        
        double multiplier = 1.0;
        if (lower.contains("млн") || lower.matches(".*\\b[m]\\b.*") || lower.contains(" m")) {
            multiplier = 1_000_000.0;
        } else if (lower.contains("тыс") || lower.contains("k") || lower.contains(" к")) {
            multiplier = 1_000.0;
        } else if (lower.contains("млрд") || lower.contains("b")) {
            multiplier = 1_000_000_000.0;
        }
        
        String cleanNum = lower.replaceAll("[^0-9.,]", "");
        if (cleanNum.isEmpty()) return -1;
        
        if (cleanNum.contains(",") && cleanNum.contains(".")) {
            cleanNum = cleanNum.replace(",", "");
        } else if (cleanNum.contains(",")) {
            int commaIdx = cleanNum.indexOf(",");
            if (cleanNum.length() - 1 - commaIdx == 3) {
                cleanNum = cleanNum.replace(",", "");
            } else {
                cleanNum = cleanNum.replace(",", ".");
            }
        }
        
        try {
            double val = Double.parseDouble(cleanNum);
            return (long) (val * multiplier);
        } catch (NumberFormatException e) {
            return -1;
        }
    }

    private void cancelPendingNavigation() {
        if (pendingNavigationTask != null && !pendingNavigationTask.isDone()) {
            pendingNavigationTask.cancel(false);
            pendingNavigationTask = null;
        }
    }

    public void handleServerMessage(String message) {
        try {
            JsonObject packet = JsonParser.parseString(message).getAsJsonObject();
            String type = packet.get("type").getAsString();
            
            if ("ACTION_COMMAND".equals(type)) {
                JsonObject payload = packet.getAsJsonObject("payload");
                String action = payload.get("action").getAsString();
                
                if ("CLICK_SLOT".equals(action)) {
                    int slotId = payload.get("slot").getAsInt();
                    String expectedItem = payload.get("expected_item").getAsString();
                    int delayMs = payload.get("delay_ms").getAsInt();

                    // Замораживаем навигацию до получения GUI подтверждения
                    isBuying = true;
                    ticksSinceLastPageUpdate = 0;
                    cancelPendingNavigation();
                    scheduleClick(slotId, expectedItem, delayMs);

                } else if ("NAVIGATE".equals(action)) {
                    // Python явно управляет переходом на следующую/предыдущую страницу.
                    String direction = payload.has("direction") ? payload.get("direction").getAsString() : "NEXT";
                    MinecraftClient navClient = MinecraftClient.getInstance();
                    navClient.execute(() -> {
                        if (navClient.player == null
                                || !(navClient.currentScreen instanceof HandledScreen<?> hs)) return;
                        if (!(hs.getScreenHandler() instanceof GenericContainerScreenHandler ch)) return;

                        if ("REFRESH".equals(direction)) {
                            System.out.println("[MarwyBot] NAVIGATE REFRESH — closing AH.");
                            scheduleMarketRefresh(ch.syncId);
                        } else {
                            int targetSlot = "PREV".equals(direction) ? 48 : 50;
                            System.out.println("[MarwyBot] NAVIGATE " + direction + " (slot " + targetSlot + ").");
                            // Случайная задержка 100–400мс (имитация человека)
                            long delay = 100 + (long)(Math.random() * 300);
                            pendingNavigationTask = clickScheduler.schedule(() ->
                                navClient.execute(() -> {
                                    if (navClient.player != null) clickSlot(ch.syncId, targetSlot, null);
                                }), delay, TimeUnit.MILLISECONDS);
                        }
                    });

                } else if ("SELL_ITEM".equals(action)) {
                    String itemId = payload.get("item_id").getAsString();
                    int price = payload.get("price").getAsInt();

                    cancelPendingNavigation();
                    sellItem(itemId, price);
                } else if ("OPEN_AH".equals(action)) {
                    System.out.println("[MarwyBot] Received OPEN_AH command from server.");
                    isBuying = false;  // цикл покупки завершён
                    isBotInitiatedScan = true;
                    MinecraftClient.getInstance().execute(() -> {
                        if (MinecraftClient.getInstance().player != null) {
                            MinecraftClient.getInstance().player.networkHandler.sendCommand("ah");
                        }
                    });
                } else if ("TOGGLE_SCANNER".equals(action)) {
                    boolean enabled = payload.get("enabled").getAsBoolean();
                    System.out.println("[MarwyBot] Setting scanner enabled to: " + enabled);
                    isScannerEnabled = enabled;
                    if (!enabled) {
                        cancelPendingNavigation();
                    }
                }
            }
        } catch (Exception e) {
            System.err.println("[MarwyBot] Error parsing server message: " + e.getMessage());
        }
    }

    private void scheduleClick(int slotId, String expectedItem, int delayMs) {
        clickScheduler.schedule(() -> {
            MinecraftClient client = MinecraftClient.getInstance();
            client.execute(() -> {
                if (client.player == null || !(client.currentScreen instanceof HandledScreen<?> handledScreen)) {
                    System.err.println("[MarwyBot] Click aborted: no screen open.");
                    return;
                }

                ScreenHandler handler = handledScreen.getScreenHandler();
                if (slotId < 0 || slotId >= handler.slots.size()) {
                    System.err.println("[MarwyBot] Click aborted: invalid slot id " + slotId);
                    return;
                }

                Slot slot = handler.getSlot(slotId);
                ItemStack stack = slot.getStack();
                // Registry возвращает только базовый id, например "minecraft:netherite_sword".
                // expectedItem может содержать суффикс зачарований через второй двоеточие,
                // например "minecraft:netherite_sword:sharpness_5,mending_1".
                // Клик разрешён если expectedItem начинается с базового id.
                String baseItemId = Registries.ITEM.getId(stack.getItem()).toString();

                if (!expectedItem.startsWith(baseItemId)) {
                    System.err.println("[MarwyBot] Click aborted: item mismatch."
                            + " Expected prefix: " + baseItemId
                            + " | Full expected: " + expectedItem);
                    return;
                }

                System.out.println("[MarwyBot] Clicking slot " + slotId + " (" + baseItemId + ")");
                client.interactionManager.clickSlot(
                    handler.syncId,
                    slotId,
                    0, // Левая кнопка
                    SlotActionType.PICKUP,
                    client.player
                );
            });
        }, delayMs, TimeUnit.MILLISECONDS);
    }

    private void detectAndSendConfirmation(GenericContainerScreenHandler handler, String screenTitle) {
        String currentSignature = "confirm:" + screenTitle;
        if (currentSignature.equals(lastSentSignature)) {
            return;
        }
        lastSentSignature = currentSignature;

        // Разметка GUI подтверждения:
        //   Левый 3x3  = lime_stained_glass_pane  = КУПИТЬ
        //   Центр      = предмет
        //   Правый 3x3 = red_stained_glass_pane   = ОТМЕНА
        //
        // Берём первую lime_stained_glass_pane в области сундука (не инвентарь).
        // Она всегда в левых колонках — это и есть кнопка покупки.

        int confirmSlot = -1;
        String confirmItemId = "";

        // Проход 1: ищем lime_stained_glass_pane (основная кнопка покупки)
        for (Slot slot : handler.slots) {
            if (slot.inventory instanceof PlayerInventory) continue;
            ItemStack stack = slot.getStack();
            if (stack.isEmpty()) continue;

            String itemId = Registries.ITEM.getId(stack.getItem()).toString();
            if (itemId.equals("minecraft:lime_stained_glass_pane")) {
                confirmSlot = slot.id;
                confirmItemId = itemId;
                break;
            }
        }

        // Проход 2 (запасной): ищем по ключевым словам в названии
        if (confirmSlot == -1) {
            for (Slot slot : handler.slots) {
                if (slot.inventory instanceof PlayerInventory) continue;
                ItemStack stack = slot.getStack();
                if (stack.isEmpty()) continue;

                String displayName = stack.getName().getString().toLowerCase();
                if (displayName.contains("подтверд") || displayName.contains("confirm")
                        || displayName.contains("купить") || displayName.contains("yes")) {
                    confirmSlot = slot.id;
                    confirmItemId = Registries.ITEM.getId(stack.getItem()).toString();
                    break;
                }
            }
        }

        if (confirmSlot != -1) {
            JsonObject packet = new JsonObject();
            packet.addProperty("type", "CONFIRM_GUI");

            JsonObject payload = new JsonObject();
            payload.addProperty("title", screenTitle);
            payload.addProperty("confirm_slot", confirmSlot);
            payload.addProperty("expected_item", confirmItemId);
            packet.add("payload", payload);

            System.out.println("[MarwyBot] Confirmation GUI: slot=" + confirmSlot
                    + " item=" + confirmItemId);
            WebsocketClientConnector.getInstance().send(GSON.toJson(packet));
        } else {
            // Логируем все слоты для диагностики неизвестной разметки GUI
            StringBuilder sb = new StringBuilder();
            sb.append("[MarwyBot] Confirmation GUI — NO lime pane found! title='")
              .append(screenTitle).append("' Chest slots: ");
            for (Slot slot : handler.slots) {
                if (slot.inventory instanceof PlayerInventory) continue;
                ItemStack stack = slot.getStack();
                if (!stack.isEmpty()) {
                    sb.append(slot.id).append(":")
                      .append(Registries.ITEM.getId(stack.getItem()).toString())
                      .append(" ");
                }
            }
            System.out.println(sb);
        }
    }



    private void sellItem(String uniqueItemId, int price) {
        MinecraftClient client = MinecraftClient.getInstance();
        if (client.player == null) return;

        client.execute(() -> {
            PlayerInventory inventory = client.player.getInventory();

            // Ищем лучший подходящий слот:
            // Проход 1: точное совпадение unique_item_id (с суффиксом зачарований / :unbreakable)
            // Проход 2: совпадение по базовому item_id (до третьего двоеточия)
            String baseItemId = extractBaseItemId(uniqueItemId);

            int bestSlot = -1;
            int bestCount = Integer.MAX_VALUE; // предпочитаем слот с наименьшим стеком

            for (int i = 0; i < inventory.main.size(); i++) {
                ItemStack stack = inventory.main.get(i);
                if (stack.isEmpty()) continue;

                String stackUniqueId = getUniqueItemId(stack,
                        Registries.ITEM.getId(stack.getItem()).toString(),
                        toLegacyText(stack.getName()),
                        extractLore(stack));

                // Приоритет — точное совпадение unique_id
                if (stackUniqueId.equals(uniqueItemId)) {
                    if (stack.getCount() < bestCount) {
                        bestSlot = i;
                        bestCount = stack.getCount();
                    }
                }
            }

            // Проход 2: по базовому id (только если точного совпадения нет)
            if (bestSlot == -1) {
                for (int i = 0; i < inventory.main.size(); i++) {
                    ItemStack stack = inventory.main.get(i);
                    if (stack.isEmpty()) continue;
                    String stackBaseId = Registries.ITEM.getId(stack.getItem()).toString();
                    if (stackBaseId.equals(baseItemId)) {
                        if (stack.getCount() < bestCount) {
                            bestSlot = i;
                            bestCount = stack.getCount();
                        }
                    }
                }
            }

            if (bestSlot == -1) {
                System.err.println("[MarwyBot] Aborting sell: item " + uniqueItemId + " not found in player inventory.");
                return;
            }

            System.out.println("[MarwyBot] Found item to sell at inventory slot: " + bestSlot + " (count: " + bestCount + ")");

            // Перемещаем в хотбар если нужно
            if (bestSlot >= 0 && bestSlot < 9) {
                inventory.selectedSlot = bestSlot;
                System.out.println("[MarwyBot] Selected hotbar slot: " + bestSlot);
            } else {
                inventory.selectedSlot = 0;
                System.out.println("[MarwyBot] Swapping inventory slot " + bestSlot + " to hotbar slot 0.");
                client.interactionManager.clickSlot(
                    client.player.playerScreenHandler.syncId,
                    bestSlot,
                    0,
                    SlotActionType.SWAP,
                    client.player
                );
            }

            // Выполняем /ah sell с небольшой задержкой
            clickScheduler.schedule(() -> {
                client.execute(() -> {
                    System.out.println("[MarwyBot] Running command: /ah sell " + price);
                    client.player.networkHandler.sendCommand("ah sell " + price);
                });
            }, 300, TimeUnit.MILLISECONDS);
        });
    }

    /**
     * Извлекает базовый Minecraft item id из unique_item_id.
     * Например: "minecraft:enchanted_book:sharpness_5" -> "minecraft:enchanted_book"
     *           "minecraft:diamond_sword:sharpness_5,unbreaking_3:unbreakable" -> "minecraft:diamond_sword"
     */
    private String extractBaseItemId(String uniqueItemId) {
        if (uniqueItemId == null) return "";
        // Базовый id — всё до третьего сегмента двоеточия
        // "minecraft:item_type" содержит ровно одно двоеточие
        int firstColon = uniqueItemId.indexOf(':');
        if (firstColon < 0) return uniqueItemId;
        int secondColon = uniqueItemId.indexOf(':', firstColon + 1);
        if (secondColon < 0) return uniqueItemId; // уже базовый
        return uniqueItemId.substring(0, secondColon);
    }



    private List<String> extractLore(ItemStack stack) {
        List<String> lore = new ArrayList<>();
        if (stack.hasNbt() && stack.getNbt().contains("display", 10)) {
            NbtCompound display = stack.getNbt().getCompound("display");
            if (display.contains("Lore", 9)) {
                NbtList loreList = display.getList("Lore", 8);
                for (int i = 0; i < loreList.size(); i++) {
                    String lineJson = loreList.getString(i);
                    try {
                        Text text = Text.Serializer.fromJson(lineJson);
                        if (text != null) {
                            lore.add(toLegacyText(text));
                        }
                    } catch (Exception e) {
                        lore.add(lineJson);
                    }
                }
            }
        }
        return lore;
    }

    private int parsePrice(List<String> lore) {
        for (String line : lore) {
            String cleanLine = line.replaceAll("[§§][0-9a-fK-Ok-orR]", "").toLowerCase().trim();
            // Ищем строку с ценой
            if (cleanLine.contains("цена") || cleanLine.contains("price") || cleanLine.contains("$") || cleanLine.contains(" coins") || cleanLine.contains("монет")) {
                // Оставляем только цифры
                String digitsOnly = cleanLine.replaceAll("[^0-9]", "");
                if (!digitsOnly.isEmpty()) {
                    try {
                        return Integer.parseInt(digitsOnly);
                    } catch (NumberFormatException e) {
                        // пропускаем
                    }
                }
            }
        }
        return 0;
    }

    private String extractSeller(List<String> lore) {
        for (String line : lore) {
            String cleanLine = line.replaceAll("[§§][0-9a-fK-Ok-orR]", "").toLowerCase().trim();
            if (cleanLine.contains("продавец") || cleanLine.contains("seller") || cleanLine.contains("владелец") || cleanLine.contains("owner")) {
                // Берём текст после двоеточия
                int colonIndex = line.indexOf(":");
                if (colonIndex != -1 && colonIndex < line.length() - 1) {
                    return line.substring(colonIndex + 1).replaceAll("[§§][0-9a-fK-Ok-orR]", "").trim();
                }
            }
        }
        return "Unknown";
    }

    /**
     * Парсит "Истекает через" / "Expires in" из lore и возвращает остаток в секундах.
     *
     * Поддерживает русский формат: "Xд. Xч. Xмин. Xсек."
     * и английский: "Xd Xh Xm Xs" — любое подмножество компонентов.
     *
     * Возвращает -1 если строка не найдена или не распарсилась.
     */
    private long parseExpirySeconds(List<String> lore) {
        for (String line : lore) {
            // Сначала убираем цветовые коды
            String clean = line.replaceAll("[\u00a7§][0-9a-fk-orA-FK-OR]", "").trim();
            String lower = clean.toLowerCase();

            // Ищем метку срока истечения
            if (!lower.contains("истекает") && !lower.contains("expires") && !lower.contains("expir")) {
                continue;
            }

            long total = 0;

            // Дни: "Xд." или "Xd"
            Matcher mDays = Pattern.compile("(\\d+)\\s*[дd][.\\s]").matcher(lower);
            if (mDays.find()) total += Long.parseLong(mDays.group(1)) * 86400L;

            // Часы: "Xч." или "Xh"
            Matcher mHours = Pattern.compile("(\\d+)\\s*[чh][.\\s]").matcher(lower);
            if (mHours.find()) total += Long.parseLong(mHours.group(1)) * 3600L;

            // Минуты: "Xмин." или "Xm"
            Matcher mMins = Pattern.compile("(\\d+)\\s*(?:мин|min|м\\.|m)[.\\s]").matcher(lower);
            if (mMins.find()) total += Long.parseLong(mMins.group(1)) * 60L;

            // Секунды: "Xсек." или "Xs"
            Matcher mSecs = Pattern.compile("(\\d+)\\s*(?:сек|sec|с\\.|s)[.\\s]").matcher(lower);
            if (mSecs.find()) total += Long.parseLong(mSecs.group(1));

            if (total > 0) {
                return total;
            }
            // Метка есть, но времени не нашли — лот истёк
            return 0;
        }
        return -1; // Строка срока не найдена
    }

    private String toLegacyText(Text text) {
        if (text == null) return "";
        try {
            String jsonStr = Text.Serializer.toJson(text);
            String legacy = jsonToLegacy(jsonStr);
            if (legacy == null || legacy.trim().isEmpty()) {
                return text.getString();
            }
            return legacy;
        } catch (Exception e) {
            return text.getString();
        }
    }

    private String jsonToLegacy(String jsonStr) {
        try {
            com.google.gson.JsonElement element = JsonParser.parseString(jsonStr);
            StringBuilder builder = new StringBuilder();
            buildLegacyText(element, builder);
            return builder.toString();
        } catch (Exception e) {
            return "";
        }
    }

    private void buildLegacyText(com.google.gson.JsonElement element, StringBuilder builder) {
        if (element == null) return;
        if (element.isJsonPrimitive()) {
            builder.append(element.getAsString());
            return;
        }
        if (element.isJsonObject()) {
            JsonObject obj = element.getAsJsonObject();
            
            // Добавляем коды стиля
            StringBuilder styleCodes = new StringBuilder();
            if (obj.has("color")) {
                String colorName = obj.get("color").getAsString();
                String code = getColorCode(colorName);
                if (code != null) {
                    styleCodes.append("§").append(code);
                }
            }
            if (obj.has("bold") && obj.get("bold").getAsBoolean()) styleCodes.append("§l");
            if (obj.has("italic") && obj.get("italic").getAsBoolean()) styleCodes.append("§o");
            if (obj.has("underlined") && obj.get("underlined").getAsBoolean()) styleCodes.append("§n");
            if (obj.has("strikethrough") && obj.get("strikethrough").getAsBoolean()) styleCodes.append("§m");
            if (obj.has("obfuscated") && obj.get("obfuscated").getAsBoolean()) styleCodes.append("§k");
            
            builder.append(styleCodes);

            if (obj.has("text")) {
                builder.append(obj.get("text").getAsString());
            }

            if (obj.has("extra")) {
                com.google.gson.JsonArray extra = obj.getAsJsonArray("extra");
                for (com.google.gson.JsonElement subElement : extra) {
                    buildLegacyText(subElement, builder);
                }
            }
        }
    }

    private String getColorCode(String colorName) {
        if (colorName == null) return null;
        colorName = colorName.toLowerCase();
        switch (colorName) {
            case "black": return "0";
            case "dark_blue": return "1";
            case "dark_green": return "2";
            case "dark_aqua": return "3";
            case "dark_red": return "4";
            case "dark_purple": return "5";
            case "gold": return "6";
            case "gray": return "7";
            case "dark_gray": return "8";
            case "blue": return "9";
            case "green": return "a";
            case "aqua": return "b";
            case "red": return "c";
            case "light_purple": return "d";
            case "yellow": return "e";
            case "white": return "f";
            default: return null;
        }
    }

    private String getUniqueItemId(ItemStack stack, String baseItemId, String displayName, List<String> lore) {
        if ("minecraft:enchanted_book".equals(baseItemId)) {
            return getEnchantedBookUniqueId(stack, baseItemId);
        }
        
        if ("minecraft:elytra".equals(baseItemId) || baseItemId.contains("helmet") || baseItemId.contains("chestplate") || baseItemId.contains("leggings") || baseItemId.contains("boots") || baseItemId.contains("sword") || baseItemId.contains("pickaxe") || baseItemId.contains("axe") || baseItemId.contains("shovel")) {
            String enchantSuffix = getEnchantmentsSuffix(stack);
            
            String customFlag = "";
            String cleanDisplay = displayName.replaceAll("§[0-9a-fk-or]", "").trim().toLowerCase();
            if (cleanDisplay.contains("неразруш") || cleanDisplay.contains("unbreakable") || (stack.hasNbt() && stack.getNbt().getBoolean("Unbreakable"))) {
                customFlag = ":unbreakable";
            }
            
            return baseItemId + enchantSuffix + customFlag;
        }
        
        return baseItemId;
    }

    private String getEnchantedBookUniqueId(ItemStack stack, String baseItemId) {
        // 1. Стандартный NBT StoredEnchantments (ваниль + большинство плагинов)
        if (stack.hasNbt()) {
            NbtCompound nbt = stack.getNbt();
            if (nbt.contains("StoredEnchantments", 9)) {
                NbtList enchants = nbt.getList("StoredEnchantments", 10);
                List<String> enchantStrings = new ArrayList<>();
                for (int i = 0; i < enchants.size(); i++) {
                    NbtCompound ench = enchants.getCompound(i);
                    String id = ench.getString("id").replace("minecraft:", "");
                    int lvl = ench.getInt("lvl");
                    enchantStrings.add(id + "_" + lvl);
                }
                if (!enchantStrings.isEmpty()) {
                    enchantStrings.sort(String::compareTo);
                    return baseItemId + ":" + String.join(",", enchantStrings);
                }
            }
        }

        // 2. Запасной вариант: используем display name как различитель.
        //    Многие плагины кастомных зачарований переименовывают книгу в название зачарования
        //    (например "§6Sharpness V" или "Острота V") вместо NBT.
        String rawName = toLegacyText(stack.getName());
        // Убираем коды цвета/форматирования
        String cleanName = rawName.replaceAll("§[0-9a-fk-orA-FK-OR]", "").trim();
        String lowerName = cleanName.toLowerCase();

        // Используем display name только если он отличается от стандартных вариантов
        if (!lowerName.isEmpty()
                && !lowerName.equals("enchanted book")
                && !lowerName.equals("зачарованная книга")
                && !lowerName.equals("enchantment book")) {
            // Преобразуем в стабильный slug: буквы/цифры (включая кириллицу), остальное → "_"
            String slug = cleanName.toLowerCase()
                    .replaceAll("[^a-z0-9а-яёa-z]", "_")
                    .replaceAll("_+", "_")
                    .replaceAll("^_|_$", "");
            if (!slug.isEmpty()) {
                return baseItemId + ":name_" + slug;
            }
        }

        return baseItemId;
    }


    private String getEnchantmentsSuffix(ItemStack stack) {
        if (!stack.hasNbt()) return "";
        NbtCompound nbt = stack.getNbt();
        NbtList enchants = null;
        if (nbt.contains("Enchantments", 9)) {
            enchants = nbt.getList("Enchantments", 10);
        } else if (nbt.contains("ench", 9)) {
            enchants = nbt.getList("ench", 10);
        }
        
        if (enchants != null && enchants.size() > 0) {
            List<String> enchantStrings = new ArrayList<>();
            for (int i = 0; i < enchants.size(); i++) {
                NbtCompound ench = enchants.getCompound(i);
                String id = ench.getString("id").replace("minecraft:", "");
                int lvl = ench.getInt("lvl");
                enchantStrings.add(id + "_" + lvl);
            }
            if (!enchantStrings.isEmpty()) {
                enchantStrings.sort(String::compareTo);
                return ":" + String.join(",", enchantStrings);
            }
        }
        return "";
    }
}
