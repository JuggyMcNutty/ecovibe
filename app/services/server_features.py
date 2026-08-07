"""What a given dedicated server can actually do.

OVH's ``/dedicated/server`` schema exposes 98 paths, but a large fraction of them
do not exist on any particular machine — they 404 or 403 depending on the
hardware tier. Verified live against a KS-C (Kimsufi): ``/features/firewall``,
``/features/kvm``, ``/features/backupCloud``, ``/biosSettings`` and ``/burst``
all return 404, and ``/install/hardwareRaidProfile`` returns 403 ("Hardware RAID
is not supported by this server"), while 26 other sub-resources answer normally.

Rendering a control panel from the schema alone would therefore give a UI full
of buttons that cannot work. Instead the optional resources are **probed once
per server** and the answer is persisted, because hardware does not change; the
frontend renders only the sections whose capability is present.

The registry below is the single source of truth for both the probe and the
read-only resource endpoint, mirroring the ``APP_SETTINGS`` registry in
``app/services/app_settings.py``.
"""
import logging
from dataclasses import dataclass, field
from typing import Any

from app.services.ovh_service import OVHServiceError

logger = logging.getLogger(__name__)

# How long a probe result is trusted. Hardware capabilities are effectively
# static, so this only exists to pick up OVH adding a feature to a range.
CAPABILITY_TTL_DAYS = 7

# Status codes that mean "this server genuinely does not have the feature", as
# opposed to a transient failure. 404 = resource absent, 403 = present in the
# API but not permitted for this hardware (OVH uses both).
_ABSENT_STATUS = (403, 404)


@dataclass(frozen=True)
class ServerResource:
    """One readable sub-resource of ``/dedicated/server/{serviceName}``."""

    key: str                              # stable id used by the API + frontend
    path: str                             # subpath ('' = the server object)
    label: str
    optional: bool = False                # True → probe; False → always present
    params: tuple[str, ...] = field(default_factory=tuple)  # allowed query params


# Read-only resources. `optional=True` entries are the ones the probe tests;
# everything else answered on every server checked and is not worth a call.
SERVER_RESOURCES: dict[str, ServerResource] = {r.key: r for r in (
    # --- always present ---
    ServerResource("hardware", "/specifications/hardware", "Hardware"),
    ServerResource("network_spec", "/specifications/network", "Network"),
    ServerResource("ip_spec", "/specifications/ip", "IP capabilities"),
    ServerResource("ips", "/ips", "IP addresses"),
    ServerResource("networking", "/networking", "Networking"),
    ServerResource("boot", "/boot", "Netboot options"),
    ServerResource("options", "/option", "Options"),
    ServerResource("ongoing", "/ongoing", "Ongoing operations"),
    ServerResource("service_infos", "/serviceInfos", "Service"),
    ServerResource("intervention", "/intervention", "Intervention history"),
    ServerResource("planned_intervention", "/plannedIntervention", "Planned interventions"),
    ServerResource("planned_change", "/plannedChange", "Planned changes"),
    ServerResource("nic", "/networkInterfaceController", "Network interfaces"),
    ServerResource("vni", "/virtualNetworkInterface", "Virtual network interfaces"),
    ServerResource("virtual_mac", "/virtualMac", "Virtual MACs"),
    ServerResource("secondary_dns", "/secondaryDnsDomains", "Secondary DNS"),
    ServerResource("spla", "/spla", "SPLA licences"),
    ServerResource("vrack", "/vrack", "vRack"),
    ServerResource("templates", "/install/compatibleTemplates", "Install templates"),
    ServerResource("windows_licences", "/license/compliantWindows", "Windows licences"),
    # mrtg needs both params; OVH 400s without them.
    ServerResource("mrtg", "/mrtg", "Traffic", params=("period", "type")),
    ServerResource("orderable_ip", "/orderable/ip", "Orderable IPs"),
    ServerResource("orderable_bandwidth", "/orderable/bandwidth", "Orderable bandwidth"),
    ServerResource("orderable_kvm", "/orderable/kvm", "KVM orderable"),

    # --- probed: absent on entry-level hardware ---
    ServerResource("ipmi", "/features/ipmi", "IPMI / KVM", optional=True),
    ServerResource("firewall", "/features/firewall", "Firewall", optional=True),
    ServerResource("kvm", "/features/kvm", "KVM", optional=True),
    ServerResource("backup_cloud", "/features/backupCloud", "Cloud backup", optional=True),
    ServerResource("backup_offer", "/backupCloudOfferDetails", "Cloud backup offer", optional=True),
    ServerResource("bios", "/biosSettings", "BIOS settings", optional=True),
    ServerResource("burst", "/burst", "Burst", optional=True),
    ServerResource("hardware_raid", "/install/hardwareRaidProfile", "Hardware RAID", optional=True),
)}

PROBED_KEYS = tuple(r.key for r in SERVER_RESOURCES.values() if r.optional)


def probe_server_capabilities(service, service_name: str) -> dict[str, bool]:
    """Return ``{key: available}`` for every optional resource.

    Only the ``optional`` entries are fetched — around eight calls. That cap
    matters: every OVH call serialises on the account's client lock
    (``OVHService._call``), so probing all 30 resources would make opening a
    server noticeably slow for no new information.

    A 403/404 is a real answer ("not on this hardware") and is cached. **Any
    other failure is omitted from the map entirely**, not recorded as False —
    a timeout or a 500 must not permanently hide a feature the server has.
    """
    caps: dict[str, bool] = {}
    for key in PROBED_KEYS:
        res = SERVER_RESOURCES[key]
        try:
            service.server_get(service_name, res.path)
            caps[key] = True
        except OVHServiceError as e:
            if e.status_code in _ABSENT_STATUS:
                caps[key] = False
            else:
                logger.debug(
                    "capability probe inconclusive for %s%s: %s",
                    service_name, res.path, e,
                )
    return caps


def derive_capabilities(caps: dict[str, bool], ipmi: dict[str, Any] | None) -> dict[str, Any]:
    """Fold the free capability detail into the probed map.

    ``/features/ipmi`` already reports exactly which console types work, so the
    UI must read that instead of assuming IPMI means a browser console. On the
    KS-C checked, ``activated`` is true but only ``kvmipJnlp`` is supported —
    no HTML5 KVM and no Serial-over-LAN — so offering those buttons would give
    three dead controls out of four.
    """
    out: dict[str, Any] = dict(caps)
    features = (ipmi or {}).get("supportedFeatures") or {}
    out["ipmi_features"] = {
        k: bool(v) for k, v in features.items()
    } if features else {}
    out["ipmi_activated"] = bool((ipmi or {}).get("activated")) if ipmi else False
    return out
