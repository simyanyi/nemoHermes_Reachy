import pytest

from reachy_bridge.cli import policy_fragment


def test_policy_scopes_host_address_binary_and_http_paths():
    rule = policy_fragment("host.openshell.internal", ["172.17.0.1"])["network_policies"][
        "reachy_output"
    ]
    endpoint = rule["endpoints"][0]
    assert endpoint["allowed_ips"] == ["172.17.0.1/32"]
    assert endpoint["enforcement"] == "enforce"
    methods = {r["allow"]["method"] for r in endpoint["rules"]}
    assert methods == {"GET", "POST"}
    assert not any(r["allow"]["path"] == "/**" for r in endpoint["rules"])
    with pytest.raises(ValueError):
        policy_fragment("host.openshell.internal", ["not-an-ip"])
