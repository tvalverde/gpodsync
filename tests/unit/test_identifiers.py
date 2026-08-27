"""Device identifiers, which arrive from the client and end up in URLs."""

import pytest

from gpodsync.domain.identifiers import InvalidDeviceId, validate_device_id

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", ["phone", "my-phone", "my_phone.2", "a", "9" * 64])
def test_accepts_ordinary_identifiers(value):
    assert validate_device_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 65,
        # Unicode word characters. Not dangerous for this sink, but confusable:
        # two devices that look identical in a log line is a bad afternoon.
        "phone\u0660\u0661",  # Arabic-Indic digits
        "\u0442\u0435\u043b\u0435\u0444\u043e\u043d",  # Cyrillic
        "\uff50\uff48\uff4f\uff4e\uff45",  # fullwidth Latin, indistinguishable at a glance
        "\U0001d400bc",
        # Nothing is legitimately called this.
        ".",
        "..",
        "../etc/passwd",
        "with space",
        "semi;colon",
        "new\nline",
        "quote'",
        "slash/es",
        "percent%20",
    ],
)
def test_rejects_anything_that_would_have_to_be_escaped_downstream(value):
    with pytest.raises(InvalidDeviceId):
        validate_device_id(value)
