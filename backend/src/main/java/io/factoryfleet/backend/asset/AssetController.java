package io.factoryfleet.backend.asset;

import java.util.List;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import jakarta.validation.Valid;

@RestController
@RequestMapping("/api/v1/sites/{siteId}/assets")
class AssetController {

    private final AssetRegistry registry;

    AssetController(AssetRegistry registry) {
        this.registry = registry;
    }

    /**
     * Registers a machine, or updates it if it has reported before.
     *
     * <p>Returns 200 rather than 201 because this is an idempotent upsert — an agent restarting
     * mid-shift calls this on every startup and should not care whether it is the first time.
     */
    @PostMapping
    AssetResponse register(@PathVariable String siteId,
                           @Valid @RequestBody RegisterAssetRequest request) {
        Asset asset = registry.register(
                siteId,
                request.assetId(),
                request.machineType(),
                request.firmwareVersion(),
                request.sensors());
        return AssetResponse.from(asset);
    }

    @GetMapping
    List<AssetResponse> listBySite(@PathVariable String siteId) {
        return registry.findBySite(siteId).stream()
                .map(AssetResponse::from)
                .toList();
    }

    @GetMapping("/{assetId}")
    AssetResponse get(@PathVariable String siteId, @PathVariable String assetId) {
        return registry.find(siteId, assetId)
                .map(AssetResponse::from)
                .orElseThrow(() -> new AssetNotFoundException(siteId, assetId));
    }
}
