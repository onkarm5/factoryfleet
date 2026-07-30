package io.factoryfleet.backend.asset;

/**
 * Health state of an asset, derived from its telemetry.
 *
 * <p>{@code DEGRADED} is the predictive-maintenance signal: readings have drifted from the
 * machine's own baseline but are still inside its absolute limits, so the machine is still
 * running and can be serviced at the next planned window. {@code CRITICAL} means a hard limit
 * has been breached.
 */
public enum AssetHealth {
    /** Registered, but has not reported telemetry yet. */
    UNKNOWN,
    /** All sensors within their normal operating baseline. */
    HEALTHY,
    /** A sensor is drifting from its baseline — investigate at next planned maintenance. */
    DEGRADED,
    /** A sensor has breached a hard limit — needs immediate attention. */
    CRITICAL,
    /** No telemetry received within the staleness threshold. */
    OFFLINE
}
