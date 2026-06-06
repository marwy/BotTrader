package org.marwy.marwy.client;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;

public class MarwyClient implements ClientModInitializer {

    @Override
    public void onInitializeClient() {
        System.out.println("[MarwyBot] Initializing Client Mod...");
        
        // Connect to local WebSocket backend server
        WebsocketClientConnector.getInstance().connect();

        // Listen for client ticks to drive periodic commands & GUI scanning
        ClientTickEvents.END_CLIENT_TICK.register(client -> {
            BotController.getInstance().onClientTick(client);
        });
    }
}
