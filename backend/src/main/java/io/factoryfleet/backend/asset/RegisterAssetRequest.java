package io.factoryfleet.backend.asset;

import java.util.List;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.NotNull;

/**
 * What an agent declares about its machine on startup.
 *
 * <p>The site comes from the path, not the body, so an agent cannot register itself into a
 * different site than the one it addressed.
 */
public record RegisterAssetRequest(

        @NotBlank
        String assetId,

        @NotNull
        MachineType machineType,

        @NotBlank
        String firmwareVersion,

        /** Sensor ids this agent will report; an asset that reports nothing is not useful. */
        @NotEmpty
        List<@NotBlank String> sensors) {
}
