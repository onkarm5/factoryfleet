package io.factoryfleet.backend.asset;

import java.time.Instant;
import java.util.List;

/** An asset as returned by the API. */
public record AssetResponse(
        String siteId,
        String assetId,
        MachineType machineType,
        String firmwareVersion,
        List<String> sensors,
        AssetHealth health,
        Instant registeredAt,
        Instant lastSeenAt) {

    static AssetResponse from(Asset asset) {
        return new AssetResponse(
                asset.siteId(),
                asset.assetId(),
                asset.machineType(),
                asset.firmwareVersion(),
                asset.sensors(),
                asset.health(),
                asset.registeredAt(),
                asset.lastSeenAt());
    }
}
