package io.factoryfleet.backend.asset;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.ResponseStatus;

@ResponseStatus(HttpStatus.NOT_FOUND)
public class AssetNotFoundException extends RuntimeException {

    public AssetNotFoundException(String siteId, String assetId) {
        super("No asset '%s' registered at site '%s'".formatted(assetId, siteId));
    }
}
