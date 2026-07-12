"""Order name/price extraction tests."""
from app.api.orders import _extract_price, _extract_server_name, _name_from_details


def test_extract_server_name_from_order_object():
    assert _extract_server_name({"domain": "ns12345.ip-1-2-3.eu"}) == "ns12345.ip-1-2-3.eu"
    assert _extract_server_name({"description": "My server"}) == "My server"
    # Top-level OVH dedicated-server orders carry none of these → None.
    assert _extract_server_name({"orderId": 22135744, "date": "2026-01-01"}) is None


def test_name_from_details_prefers_description():
    """Regression: dedicated-server orders show their name on the line items,
    not the order object — so the list must fall back to details (was showing
    '(unknown)')."""
    details = [
        {"detailType": "INSTALLATION", "description": "", "domain": "*"},
        {"detailType": "DURATION", "description": "Dedicated Server KS-A | Intel i5",
         "domain": "ns3164033.ip-51-91-33.eu"},
    ]
    assert _name_from_details(details) == "Dedicated Server KS-A | Intel i5"


def test_name_from_details_falls_back_to_domain():
    details = [{"description": "", "domain": "ns999.ip-1-2-3.eu"}]
    assert _name_from_details(details) == "ns999.ip-1-2-3.eu"


def test_name_from_details_skips_placeholder_and_empty():
    assert _name_from_details([{"description": "", "domain": "*"}]) is None
    assert _name_from_details([]) is None


def test_extract_price():
    order = {"priceWithTax": {"priceInUcents": 5990000000, "currencyCode": "EUR"}}
    assert _extract_price(order) == (5990000000, "EUR")
    assert _extract_price({}) == (None, None)
    # Missing currency → treated as no price.
    assert _extract_price({"priceWithTax": {"priceInUcents": 100}}) == (None, None)
