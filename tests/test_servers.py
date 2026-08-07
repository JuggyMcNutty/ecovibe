"""Tests for the owned-servers and bills read endpoints."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.ovh_service import get_active_ovh_service

XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def client():
    return TestClient(app)


def _create_account(client, endpoint="ovh-us"):
    r = client.post(
        "/api/accounts",
        json={
            "label": "test", "endpoint": endpoint, "application_key": "ak",
            "application_secret": "secret123", "consumer_key": "ck",
        },
        headers=XHR,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_servers_unconfigured_returns_503(client):
    assert client.get("/api/servers").status_code == 503
    assert client.get("/api/account/bills").status_code == 503


def test_list_servers_enriched(client):
    _create_account(client)
    svc = get_active_ovh_service()
    svc.list_dedicated_servers = MagicMock(return_value=["ns1.example.net"])
    svc.get_dedicated_server = MagicMock(return_value={
        "reverse": "web1.example.com", "datacenter": "vin", "os": "debian12_64",
        "state": "ok", "commercialRange": "KS-LE-1", "ip": "1.2.3.4",
    })
    svc.get_server_service_info = MagicMock(return_value={
        "expiration": "2026-09-01", "renewalType": "automaticV2016",
    })

    r = client.get("/api/servers")
    assert r.status_code == 200
    servers = r.json()["servers"]
    assert servers == [{
        "service_name": "ns1.example.net",
        "display_name": "web1.example.com",
        "datacenter": "vin",
        "os": "debian12_64",
        "state": "ok",
        "commercial_range": "KS-LE-1",
        "ip": "1.2.3.4",
        "expiration": "2026-09-01",
        "renewal_type": "automaticV2016",
    }]


def test_list_servers_degrades_on_detail_failure(client):
    from app.services.ovh_service import OVHServiceError

    _create_account(client)
    svc = get_active_ovh_service()
    svc.list_dedicated_servers = MagicMock(return_value=["a", "b"])

    def _detail(name):
        if name == "a":
            raise OVHServiceError("boom", status_code=500)
        return {"datacenter": "vin", "state": "ok"}

    svc.get_dedicated_server = MagicMock(side_effect=_detail)
    svc.get_server_service_info = MagicMock(return_value={})

    servers = client.get("/api/servers").json()["servers"]
    assert servers[0] == {"service_name": "a"}       # degraded, not fatal
    assert servers[1]["service_name"] == "b"
    assert servers[1]["datacenter"] == "vin"


def test_server_detail(client):
    _create_account(client)
    svc = get_active_ovh_service()
    svc.get_dedicated_server = MagicMock(return_value={"state": "ok", "datacenter": "hil"})
    svc.get_server_service_info = MagicMock(return_value={"expiration": "2026-12-31"})

    r = client.get("/api/servers/ns1.example.net")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["expiration"] == "2026-12-31"
    assert body["detail"]["state"] == "ok"


# ----- capability discovery -----
#
# OVH advertises 98 paths under /dedicated/server but a given machine implements
# only some of them. Verified live on a KS-C: firewall/kvm/backupCloud/
# biosSettings/burst return 404 and hardwareRaidProfile returns 403, while 26
# other sub-resources answer normally. The UI renders sections from this map, so
# a wrong answer here means either dead buttons or hidden features.

# What the live KS-C returns, by subpath.
_KSC_ABSENT = {
    "/features/firewall": 404, "/features/kvm": 404,
    "/features/backupCloud": 404, "/backupCloudOfferDetails": 403,
    "/biosSettings": 404, "/burst": 404, "/install/hardwareRaidProfile": 403,
}
_KSC_IPMI = {
    "activated": True,
    "supportedFeatures": {
        "kvmipJnlp": True, "kvmipHtml5URL": False,
        "serialOverLanURL": False, "serialOverLanSshKey": False,
    },
}


def _fake_server_get(calls, absent=None, ipmi=None):
    from app.services.ovh_service import OVHServiceError

    absent = _KSC_ABSENT if absent is None else absent

    def _get(service_name, subpath="", **kwargs):
        calls.append(subpath)
        if subpath in absent:
            raise OVHServiceError("nope", status_code=absent[subpath])
        if subpath == "/features/ipmi":
            return _KSC_IPMI if ipmi is None else ipmi
        return {"ok": subpath, "params": kwargs}

    return _get


def test_capability_probe_matches_the_live_hardware(client):
    _create_account(client)
    svc = get_active_ovh_service()
    calls = []
    svc.server_get = MagicMock(side_effect=_fake_server_get(calls))

    body = client.get("/api/servers/ns1.example/capabilities").json()

    caps = body["capabilities"]
    assert caps["ipmi"] is True
    assert caps["firewall"] is False
    assert caps["kvm"] is False
    assert caps["backup_cloud"] is False
    assert caps["bios"] is False
    assert caps["burst"] is False
    assert caps["hardware_raid"] is False
    # /features/ipmi already says which consoles work — on this box only the
    # Java .jnlp, so the HTML5/SoL buttons must never be offered.
    assert caps["ipmi_features"] == {
        "kvmipJnlp": True, "kvmipHtml5URL": False,
        "serialOverLanURL": False, "serialOverLanSshKey": False,
    }
    assert caps["ipmi_activated"] is True
    assert body["cached"] is False


def test_probe_only_touches_optional_resources(client):
    """The probe must stay small: every OVH call serialises on the account
    lock, so probing all 32 resources would make opening a server crawl."""
    from app.services.server_features import PROBED_KEYS

    _create_account(client)
    svc = get_active_ovh_service()
    calls = []
    svc.server_get = MagicMock(side_effect=_fake_server_get(calls))

    client.get("/api/servers/ns1.example/capabilities")

    # One call per optional resource, plus one for the IPMI feature detail.
    assert len(calls) == len(PROBED_KEYS) + 1
    assert "/specifications/hardware" not in calls   # always-present: never probed


def test_inconclusive_probe_is_not_cached_as_absent(client):
    """A timeout or 500 must not permanently hide a feature the server has."""
    _create_account(client)
    svc = get_active_ovh_service()
    absent = dict(_KSC_ABSENT, **{"/features/ipmi": 500})
    svc.server_get = MagicMock(side_effect=_fake_server_get([], absent=absent))

    caps = client.get("/api/servers/ns1.example/capabilities").json()["capabilities"]

    assert "ipmi" not in caps          # omitted, not False
    assert caps["firewall"] is False   # a real 404 still records absence


def test_capabilities_are_cached_and_refreshable(client):
    _create_account(client)
    svc = get_active_ovh_service()
    calls = []
    svc.server_get = MagicMock(side_effect=_fake_server_get(calls))

    client.get("/api/servers/ns1.example/capabilities")
    first = len(calls)

    body = client.get("/api/servers/ns1.example/capabilities").json()
    assert body["cached"] is True
    assert len(calls) == first          # no extra OVH work

    body = client.get("/api/servers/ns1.example/capabilities?refresh=true").json()
    assert body["cached"] is False
    assert len(calls) > first


def test_stale_capabilities_are_reprobed(client):
    from datetime import datetime, timedelta, timezone

    from app.services.server_features import CAPABILITY_TTL_DAYS
    from app.services.storage import get_storage

    _create_account(client)
    svc = get_active_ovh_service()
    calls = []
    svc.server_get = MagicMock(side_effect=_fake_server_get(calls))
    old = datetime.now(timezone.utc) - timedelta(days=CAPABILITY_TTL_DAYS + 1)
    get_storage().save_server_capabilities(
        "ns1.example", {"firewall": True}, old, svc.account_id
    )

    body = client.get("/api/servers/ns1.example/capabilities").json()

    assert body["cached"] is False
    assert body["capabilities"]["firewall"] is False   # re-probed, corrected
    assert calls


# ----- registry-driven resource reads -----


def test_resource_endpoint_rejects_unknown_keys(client):
    """Not a passthrough: an arbitrary key must not reach an arbitrary OVH
    path, which would expose everything the credentials can touch."""
    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_get = MagicMock(side_effect=_fake_server_get([]))

    assert client.get("/api/servers/ns1.example/resource/bogus").status_code == 404
    assert client.get("/api/servers/ns1.example/resource/me").status_code == 404
    svc.server_get.assert_not_called()


def test_resource_endpoint_reads_a_known_key(client):
    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_get = MagicMock(side_effect=_fake_server_get([]))

    body = client.get("/api/servers/ns1.example/resource/hardware").json()
    assert body["key"] == "hardware"
    assert body["data"]["ok"] == "/specifications/hardware"


def test_resource_endpoint_requires_declared_params(client):
    """OVH 400s /mrtg without period+type, so ask for them up front."""
    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_get = MagicMock(side_effect=_fake_server_get([]))

    assert client.get("/api/servers/ns1.example/resource/mrtg").status_code == 422

    r = client.get(
        "/api/servers/ns1.example/resource/mrtg?period=daily&type=traffic:download"
    )
    assert r.status_code == 200
    assert r.json()["data"]["params"] == {"period": "daily", "type": "traffic:download"}


# ----- power, boot, flags -----


def test_boot_options_resolve_ids_to_details(client):
    """OVH's /boot returns bare ids; the UI needs bootType to label them."""
    _create_account(client)
    svc = get_active_ovh_service()
    boots = {
        1: {"bootType": "harddisk", "kernel": "hd", "description": "Boot to disk"},
        95083: {"bootType": "power", "kernel": "poweroff", "description": "Power-off server"},
        230242: {"bootType": "rescue", "kernel": "rescue12-customer", "description": "Rescue"},
    }

    def _get(name, subpath="", **kw):
        if subpath == "/boot":
            return list(boots)
        return boots[int(subpath.rsplit("/", 1)[1])]

    svc.server_get = MagicMock(side_effect=_get)

    options = client.get("/api/servers/ns1.example/boot").json()["options"]
    assert [o["boot_type"] for o in options] == ["harddisk", "power", "rescue"]
    assert options[2]["description"] == "Rescue"


def test_set_boot_without_reboot_does_not_reboot(client):
    """A boot change alone is not downtime; rebooting must be opt-in."""
    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_put = MagicMock(return_value=None)
    svc.server_post = MagicMock(return_value={"taskId": 1})

    body = client.put(
        "/api/servers/ns1.example/boot", json={"boot_id": 230242}, headers=XHR,
    ).json()

    assert body["boot_id"] == 230242
    assert body["rebooted"] is False
    svc.server_put.assert_called_with("ns1.example", "", bootId=230242)
    svc.server_post.assert_not_called()


def test_set_boot_with_reboot_reports_a_partial_failure(client):
    """If the boot change lands but the reboot fails, say so — a caller that
    retries blindly would re-apply the boot change."""
    from app.services.ovh_service import OVHServiceError

    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_put = MagicMock(return_value=None)
    svc.server_post = MagicMock(side_effect=OVHServiceError("boom", status_code=500))

    r = client.put(
        "/api/servers/ns1.example/boot",
        json={"boot_id": 230242, "reboot": True}, headers=XHR,
    )
    assert r.status_code == 502
    assert "Boot set to 230242" in r.json()["detail"]


def test_properties_accepts_only_writable_fields(client):
    """dedicated.server.Dedicated is mostly read-only; state/powerState/rack
    must never reach OVH."""
    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_put = MagicMock(return_value=None)

    body = client.put(
        "/api/servers/ns1.example/properties",
        json={"monitoring": False, "state": "hacked", "power_state": "poweroff",
              "rack": "elsewhere"},
        headers=XHR,
    ).json()

    assert body["updated"] == {"monitoring": False}
    svc.server_put.assert_called_once_with("ns1.example", "", monitoring=False)


def test_properties_rejects_an_empty_update(client):
    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_put = MagicMock(return_value=None)

    r = client.put(
        "/api/servers/ns1.example/properties", json={"rack": "nope"}, headers=XHR,
    )
    assert r.status_code == 422
    svc.server_put.assert_not_called()


def test_reboot_returns_the_task(client):
    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_post = MagicMock(return_value={"taskId": 42, "status": "todo"})

    body = client.post("/api/servers/ns1.example/reboot", headers=XHR).json()
    assert body["task"]["taskId"] == 42
    svc.server_post.assert_called_once_with("ns1.example", "/reboot")


# ----- tasks -----


def test_tasks_listed_newest_first_and_capped(client):
    _create_account(client)
    svc = get_active_ovh_service()

    def _get(name, subpath="", **kw):
        if subpath == "/task":
            return [1, 2, 3, 4, 5]
        return {"taskId": int(subpath.rsplit("/", 1)[1]), "status": "done"}

    svc.server_get = MagicMock(side_effect=_get)

    tasks = client.get("/api/servers/ns1.example/tasks?limit=3").json()["tasks"]
    assert [t["taskId"] for t in tasks] == [5, 4, 3]


def test_task_detail_flags_terminal_status(client):
    """The frontend polls until a task stops moving, so it needs to be told."""
    _create_account(client)
    svc = get_active_ovh_service()

    svc.server_get = MagicMock(return_value={"taskId": 7, "status": "doing"})
    assert client.get("/api/servers/ns1.example/tasks/7").json()["terminal"] is False

    svc.server_get = MagicMock(return_value={"taskId": 7, "status": "ovhError"})
    assert client.get("/api/servers/ns1.example/tasks/7").json()["terminal"] is True


# ----- reinstall -----


def test_reinstall_passes_customizations_and_storage_through(client):
    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_post = MagicMock(return_value={"taskId": 99})

    body = client.post(
        "/api/servers/ns1.example/reinstall",
        json={
            "operating_system": "debian12_64",
            "customizations": {
                "hostname": "web1", "ssh_key": "ssh-ed25519 AAAA",
                "post_installation_script": "#!/bin/sh\necho hi",
            },
            "storage": [{"disk_group_id": 0, "partitioning": {"schemeName": "default"}}],
        },
        headers=XHR,
    ).json()

    assert body["task"]["taskId"] == 99
    svc.server_post.assert_called_once_with(
        "ns1.example", "/reinstall",
        operatingSystem="debian12_64",
        customizations={
            "hostname": "web1", "sshKey": "ssh-ed25519 AAAA",
            "postInstallationScript": "#!/bin/sh\necho hi",
        },
        storage=[{"diskGroupId": 0, "partitioning": {"schemeName": "default"}}],
    )


def test_reinstall_omits_empty_optional_blocks(client):
    """An all-null customizations block must not be sent as `{}` — OVH would
    take it as an explicit empty customization."""
    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_post = MagicMock(return_value={})

    client.post(
        "/api/servers/ns1.example/reinstall",
        json={"operating_system": "debian12_64", "customizations": {}, "storage": []},
        headers=XHR,
    )
    svc.server_post.assert_called_once_with(
        "ns1.example", "/reinstall", operatingSystem="debian12_64"
    )


def test_install_status_maps_idle_404_to_not_installing(client):
    """OVH 404s when nothing is installing; that's the normal state."""
    from app.services.ovh_service import OVHServiceError

    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_get = MagicMock(
        side_effect=OVHServiceError("not being installed", status_code=404)
    )

    r = client.get("/api/servers/ns1.example/install/status")
    assert r.status_code == 200
    assert r.json() == {"installing": False, "status": None}


def test_raid_profile_reports_unsupported_instead_of_erroring(client):
    """Entry-level hardware answers 403 — an answer, not a failure."""
    from app.services.ovh_service import OVHServiceError

    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_get = MagicMock(
        side_effect=OVHServiceError("Hardware RAID is not supported", status_code=403)
    )

    r = client.get("/api/servers/ns1.example/install/raid-profile")
    assert r.status_code == 200
    assert r.json() == {"supported": False, "profile": None}


def test_install_templates_flattens_and_groups(client):
    _create_account(client)
    svc = get_active_ovh_service()
    svc.server_get = MagicMock(return_value={
        "ovh": ["debian12_64", "alma9_64"], "personal": ["mine_64"],
    })

    body = client.get("/api/servers/ns1.example/install/templates").json()
    assert body["all"] == ["alma9_64", "debian12_64", "mine_64"]
    assert body["groups"]["personal"] == ["mine_64"]


def test_bills_list_caps_and_reads_defensively(client):
    _create_account(client)
    svc = get_active_ovh_service()
    svc.list_bills = MagicMock(return_value=["B1", "B2"])
    svc.get_bill = MagicMock(side_effect=lambda bid: {
        "billId": bid, "date": "2026-06-01T00:00:00Z",
        "priceWithTax": {"value": 40.0, "text": "$40.00", "currencyCode": "USD"},
        "pdfUrl": f"https://x/{bid}.pdf", "url": f"https://x/{bid}",
    })

    r = client.get("/api/account/bills?limit=1")
    assert r.status_code == 200
    bills = r.json()["bills"]
    # Cap of 1 keeps the most recent (OVH lists newest last).
    assert len(bills) == 1
    assert bills[0]["bill_id"] == "B2"
    assert bills[0]["price_with_tax"] == 40.0
    assert bills[0]["currency_code"] == "USD"
