package io.factoryfleet.backend.asset;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.List;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class AssetRegistryTest {

    private static final Instant SHIFT_START = Instant.parse("2026-07-30T06:00:00Z");

    private MutableClock clock;
    private AssetRegistry registry;

    @BeforeEach
    void setUp() {
        clock = new MutableClock(SHIFT_START);
        registry = new AssetRegistry(clock);
    }

    @Test
    void registersNewAssetAsUnknownUntilItReportsTelemetry() {
        Asset asset = registry.register(
                "PLANT-A", "PRESS-01", MachineType.HYDRAULIC_PRESS, "2.4.1", List.of("vibration"));

        assertThat(asset.health()).isEqualTo(AssetHealth.UNKNOWN);
        assertThat(asset.registeredAt()).isEqualTo(SHIFT_START);
        assertThat(asset.lastSeenAt()).isEqualTo(SHIFT_START);
    }

    @Test
    void reRegistrationUpdatesInPlaceRatherThanCreatingADuplicate() {
        registry.register(
                "PLANT-A", "PRESS-01", MachineType.HYDRAULIC_PRESS, "2.4.1", List.of("vibration"));

        clock.advanceTo(SHIFT_START.plusSeconds(3600));
        Asset updated = registry.register(
                "PLANT-A", "PRESS-01", MachineType.HYDRAULIC_PRESS, "2.5.0",
                List.of("vibration", "temperature"));

        assertThat(registry.findBySite("PLANT-A")).hasSize(1);
        assertThat(updated.firmwareVersion()).isEqualTo("2.5.0");
        assertThat(updated.sensors()).containsExactly("vibration", "temperature");
        assertThat(updated.registeredAt()).isEqualTo(SHIFT_START);
        assertThat(updated.lastSeenAt()).isEqualTo(SHIFT_START.plusSeconds(3600));
    }

    @Test
    void assetIdsAreScopedToTheirSite() {
        registry.register("PLANT-A", "PRESS-01", MachineType.HYDRAULIC_PRESS, "2.4.1", List.of("vibration"));
        registry.register("PLANT-B", "PRESS-01", MachineType.HYDRAULIC_PRESS, "1.0.0", List.of("vibration"));

        assertThat(registry.findBySite("PLANT-A")).hasSize(1);
        assertThat(registry.find("PLANT-B", "PRESS-01"))
                .get()
                .extracting(Asset::firmwareVersion)
                .isEqualTo("1.0.0");
    }

    @Test
    void listingIsOrderedByAssetId() {
        registry.register("PLANT-A", "MILL-02", MachineType.CNC_MILL, "1.0.0", List.of("vibration"));
        registry.register("PLANT-A", "CONV-01", MachineType.CONVEYOR, "1.0.0", List.of("cycle-count"));
        registry.register("PLANT-A", "MILL-01", MachineType.CNC_MILL, "1.0.0", List.of("vibration"));

        assertThat(registry.findBySite("PLANT-A"))
                .extracting(Asset::assetId)
                .containsExactly("CONV-01", "MILL-01", "MILL-02");
    }

    @Test
    void findReturnsEmptyForAnUnknownAsset() {
        assertThat(registry.find("PLANT-A", "NOPE-99")).isEmpty();
    }

    /** Lets a test move time forward so last-seen updates are observable. */
    private static final class MutableClock extends Clock {

        private Instant now;

        private MutableClock(Instant now) {
            this.now = now;
        }

        void advanceTo(Instant later) {
            this.now = later;
        }

        @Override
        public Instant instant() {
            return now;
        }

        @Override
        public ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(ZoneId zone) {
            return this;
        }
    }
}
