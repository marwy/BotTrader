package org.marwy.marwy.client;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

public class WebsocketClientConnector {
    private static final WebsocketClientConnector INSTANCE = new WebsocketClientConnector();
    private static final String SERVER_URI = "ws://localhost:8080";
    
    private final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread thread = new Thread(r, "Marwy-Websocket-Connector");
        thread.setDaemon(true);
        return thread;
    });
    
    private WebSocket webSocket;
    private boolean isConnecting = false;

    private WebsocketClientConnector() {}

    public static WebsocketClientConnector getInstance() {
        return INSTANCE;
    }

    public synchronized void connect() {
        if (webSocket != null || isConnecting) {
            return;
        }
        isConnecting = true;
        System.out.println("[MarwyBot] Connecting to WebSocket server at " + SERVER_URI);
        
        HttpClient.newHttpClient().newWebSocketBuilder()
            .buildAsync(URI.create(SERVER_URI), new WebSocketListener())
            .thenAccept(ws -> {
                synchronized (WebsocketClientConnector.this) {
                    this.webSocket = ws;
                    this.isConnecting = false;
                    System.out.println("[MarwyBot] Successfully connected to WebSocket server.");
                }
            })
            .exceptionally(ex -> {
                synchronized (WebsocketClientConnector.this) {
                    this.isConnecting = false;
                    System.err.println("[MarwyBot] Connection failed: " + ex.getMessage());
                    scheduleReconnect();
                }
                return null;
            });
    }

    public synchronized void send(String text) {
        if (webSocket != null) {
            webSocket.sendText(text, true);
        }
    }

    private synchronized void scheduleReconnect() {
        webSocket = null;
        System.out.println("[MarwyBot] Scheduling reconnect in 5 seconds...");
        scheduler.schedule(this::connect, 5, TimeUnit.SECONDS);
    }

    private class WebSocketListener implements WebSocket.Listener {
        private final StringBuilder messageBuffer = new StringBuilder();

        @Override
        public void onOpen(WebSocket webSocket) {
            WebSocket.Listener.super.onOpen(webSocket);
        }

        @Override
        public CompletionStage<?> onText(WebSocket webSocket, CharSequence data, boolean last) {
            messageBuffer.append(data);
            if (last) {
                String completeMessage = messageBuffer.toString();
                messageBuffer.setLength(0);
                
                // Dispatch message to controller
                BotController.getInstance().handleServerMessage(completeMessage);
            }
            return WebSocket.Listener.super.onText(webSocket, data, last);
        }

        @Override
        public CompletionStage<?> onClose(WebSocket webSocket, int statusCode, String reason) {
            System.out.println("[MarwyBot] WebSocket connection closed: " + reason + " (" + statusCode + ")");
            scheduleReconnect();
            return WebSocket.Listener.super.onClose(webSocket, statusCode, reason);
        }

        @Override
        public void onError(WebSocket webSocket, Throwable error) {
            System.err.println("[MarwyBot] WebSocket error: " + error.getMessage());
            scheduleReconnect();
        }
    }
}
