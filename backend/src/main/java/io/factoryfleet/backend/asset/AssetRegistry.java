package io.factoryfleet.backend.asset;

import java.time.Clock;
import java.time.Instant;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;

import org.springframework.stereotype.Service;

/**
 * The fleet's asset registry.
 *
 * <p>Backed by an in-memory map at this milestone; PostgreSQL persistence arrives with the
 * telemetry pipeline. Callers depend only on the methods here, so the storage swap stays
 * contained to this class.
 */
@Service
public class AssetRegistry {

    private final ConcurrentMap<AssetKey, Asset> assets = new ConcurrentHashMap<>();
    private final Clock clock;

    AssetRegistry(Clock clock) {
        this.clock = clock;
    }

    /**
     * Registers an asset, or updates it if the agent has reported before. Idempotent: an agent
     * restarting re-declares itself without creating a second record.
     */
    public Asset register(String siteId,
                          String assetId,
                          MachineType machineType,
                          String firmwareVersion,
                          List<String> sensors) {
        Instant now = clock.instant();
        return assets.compute(new AssetKey(siteId, assetId), (key, existing) -> {
            if (existing == null) {
                return new Asset(siteId, assetId, machineType, firmwareVersion, sensors, now);
            }
            existing.applyRegistration(machineType, firmwareVersion, sensors, now);
            return existing;
        });
    }

    /** Assets at a site, ordered by asset id so listings are stable. */
    public List<Asset> findBySite(String siteId) {
        return assets.values().stream()
                .filter(asset -> asset.siteId().equals(siteId))
                .sorted(Comparator.comparing(Asset::assetId))
                .toList();
    }

    public Optional<Asset> find(String siteId, String assetId) {
        return Optional.ofNullable(assets.get(new AssetKey(siteId, assetId)));
    }

    private record AssetKey(String siteId, String assetId) {
    }
}
