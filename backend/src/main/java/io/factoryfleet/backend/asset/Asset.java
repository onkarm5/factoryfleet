package io.factoryfleet.backend.asset;

import java.time.Instant;
import java.util.List;

/**
 * One monitored plant-floor machine.
 *
 * <p>Identified by {@code assetId} within a {@code siteId}; the pair is unique across the fleet.
 * Identity and registration time are fixed once created — everything an agent can re-declare on
 * restart (machine type, firmware, sensor list) is mutable so re-registration updates in place
 * instead of creating a duplicate.
 */
public class Asset {

    private final String siteId;
    private final String assetId;
    private final Instant registeredAt;

    private MachineType machineType;
    private String firmwareVersion;
    private List<String> sensors;
    private AssetHealth health;
    private Instant lastSeenAt;

    public Asset(String siteId,
                 String assetId,
                 MachineType machineType,
                 String firmwareVersion,
                 List<String> sensors,
                 Instant registeredAt) {
        this.siteId = siteId;
        this.assetId = assetId;
        this.machineType = machineType;
        this.firmwareVersion = firmwareVersion;
        this.sensors = List.copyOf(sensors);
        this.registeredAt = registeredAt;
        this.lastSeenAt = registeredAt;
        this.health = AssetHealth.UNKNOWN;
    }

    /**
     * Applies a re-registration from the agent. Health is deliberately untouched: it is derived
     * from telemetry, and an agent restart is not evidence the machine became healthy.
     */
    void applyRegistration(MachineType machineType,
                           String firmwareVersion,
                           List<String> sensors,
                           Instant seenAt) {
        this.machineType = machineType;
        this.firmwareVersion = firmwareVersion;
        this.sensors = List.copyOf(sensors);
        this.lastSeenAt = seenAt;
    }

    public String siteId() {
        return siteId;
    }

    public String assetId() {
        return assetId;
    }

    public MachineType machineType() {
        return machineType;
    }

    public String firmwareVersion() {
        return firmwareVersion;
    }

    public List<String> sensors() {
        return sensors;
    }

    public AssetHealth health() {
        return health;
    }

    public Instant registeredAt() {
        return registeredAt;
    }

    public Instant lastSeenAt() {
        return lastSeenAt;
    }
}
