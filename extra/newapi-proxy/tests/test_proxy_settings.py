"""Environment parsing: strip, strict formats, fail closed without echoing values."""

import pytest

from newapi_proxy.settings import QUOTA_PER_POINT, SettingsError, load_settings
from proxy_harness import AP_TOKEN, CRON_SECRET, NOW, OP_TOKEN, VALKEY_PASSWORD, WEB_SECRET, base_env


def load(**overrides):
    return load_settings(base_env(**overrides), NOW)


def assert_rejected_without_value(secret_value: str, **overrides):
    with pytest.raises(SettingsError) as info:
        load(**overrides)
    message = str(info.value)
    assert secret_value not in message
    assert secret_value.strip() not in message
    return info.value


def test_valid_env_loads():
    settings, warnings = load()
    assert warnings == []
    assert settings.key_for("web-2026a")[0] == "web"
    assert settings.key_for("web-2026b")[0] == "web"  # next key accepted for rotation
    assert settings.key_for("cron-2026a")[0] == "cron"
    assert settings.key_for("nope") is None
    # 100,000 points per hour split across 2 replicas
    assert settings.add_quota_hourly_cap == 100_000 * QUOTA_PER_POINT // 2
    assert "Secret" not in repr(settings)


def test_values_are_stripped():
    settings, _ = load(
        PROXY_AUTH_CRON=f"  cron-x:{CRON_SECRET}\n",
        PROXY_AP_TOKEN=f"{AP_TOKEN}\n",
        PROXY_USERNAME_REGEX="  ^wp\\d{10}$ ",
    )
    assert settings.key_for("cron-x") == ("cron", CRON_SECRET.encode())
    assert settings.ap_token == AP_TOKEN
    assert settings.username_re.fullmatch("wp0000000001")


@pytest.mark.parametrize(
    "value",
    [
        "cron-x:short",  # secret < 32
        "cron-x",  # no colon
        f"cron x:{CRON_SECRET}",  # space in kid
        f"cron-x:{CRON_SECRET}\n!",  # forbidden char after newline
        f"cron-x:{CRON_SECRET[:-1]}é",  # non-ASCII
        f"cron-x:{CRON_SECRET},cron-x:{WEB_SECRET}x",  # repeated kid
    ],
)
def test_bad_key_list_fails_closed_without_value(value):
    err = assert_rejected_without_value(value, PROXY_AUTH_CRON=value)
    assert err.name == "PROXY_AUTH_CRON"
    assert CRON_SECRET not in str(err)


def test_kid_in_both_lists_rejected():
    with pytest.raises(SettingsError):
        load(PROXY_AUTH_CRON=f"web-2026a:{CRON_SECRET}")


def test_same_secret_in_web_and_cron_rejected():
    # A WEB holder must not be able to sign as CRON.
    with pytest.raises(SettingsError) as info:
        load(PROXY_AUTH_CRON=f"cron-x:{WEB_SECRET}")
    assert WEB_SECRET not in str(info.value)


def test_no_keys_at_all_rejected():
    with pytest.raises(SettingsError):
        load(PROXY_AUTH_WEB="", PROXY_AUTH_CRON="")


@pytest.mark.parametrize("value", ["nap_short", "sk-" + "a" * 48, AP_TOKEN + "\n" + "x", "Bearer " + AP_TOKEN])
def test_bad_pat_rejected_without_value(value):
    err = assert_rejected_without_value(value, PROXY_AP_TOKEN=value)
    assert err.name == "PROXY_AP_TOKEN"


def test_ap_and_op_must_differ():
    with pytest.raises(SettingsError):
        load(PROXY_OP_TOKEN=AP_TOKEN)


def test_missing_tokens_disable_identities():
    settings, _ = load(PROXY_AP_TOKEN="", PROXY_OP_TOKEN="")
    assert settings.ap_token is None and settings.op_token is None
    assert OP_TOKEN not in settings.secrets


@pytest.mark.parametrize(
    "value",
    [
        "",
        "wp\\d{10}",
        "^wp\\d{10}",
        "^(unclosed$",
        "^.*$",
        # anchored and non-empty, but wide open: the shape rule refuses them
        "^.+$",
        "^wp\\d{10}$|^root$",
        "^w.*$",
        "^(wp|ws)\\d{10}$",
        "^w[a-z]+$",
        "^wp\\S{10}$",
        "^[a-z]{2}\\d{10}$",
        "^Wp\\d{10}$",
    ],
)
def test_username_regex_validated(value):
    with pytest.raises(SettingsError):
        load(PROXY_USERNAME_REGEX=value)


@pytest.mark.parametrize("value", ["^wp\\d{10}$", "^ws[a-z]{2}\\d{10}$", "^wsab\\d{10}$", "^ws[a-z0-9]{2,3}\\d{10}$"])
def test_username_regex_real_shapes_accepted(value):
    settings, _ = load(PROXY_USERNAME_REGEX=value)
    assert settings.username_re.pattern == value


def test_username_regex_is_ascii_only():
    settings, _ = load(PROXY_USERNAME_REGEX=r"^wp\d{10}$")
    assert settings.username_re.fullmatch("wp0000000001")
    # Arabic-Indic digits must not count as \d
    assert not settings.username_re.fullmatch("wp" + "١" * 10)


@pytest.mark.parametrize(
    "value",
    ["198.18.0.0-198.19.255.255", "198.18.1.0-198.20.0.0", "10.0.0.1-10.0.0.2", "198.19.0.0-198.18.5.0", "nope"],
)
def test_tenant_range_must_stay_inside_tenant_block(value):
    with pytest.raises(SettingsError):
        load(PROXY_TENANT_IP_RANGE=value)


def test_break_glass_off_by_default_and_window_capped():
    settings, warnings = load()
    assert settings.break_glass_until is None
    settings, warnings = load(PROXY_BREAK_GLASS_UNTIL=str(NOW + 3600))
    assert settings.break_glass_until == NOW + 3600
    # More than 24 h ahead: stays off (fail closed) with a warning.
    settings, warnings = load(PROXY_BREAK_GLASS_UNTIL=str(NOW + 25 * 3600))
    assert settings.break_glass_until is None
    assert warnings == ["break_glass_until_too_far"]
    # Already in the past: off.
    settings, _ = load(PROXY_BREAK_GLASS_UNTIL=str(NOW - 1))
    assert settings.break_glass_until is None
    with pytest.raises(SettingsError):
        load(PROXY_BREAK_GLASS_UNTIL="tomorrow")


def test_expiry_accepts_unix_or_date():
    settings, _ = load(PROXY_AP_EXPIRES_AT="2027-04-14", PROXY_OP_EXPIRES_AT=str(NOW))
    assert settings.ap_expires_at == 1807660800
    assert settings.op_expires_at == NOW
    with pytest.raises(SettingsError):
        load(PROXY_AP_EXPIRES_AT="2027-13-40")


@pytest.mark.parametrize("value", ["https://newapi:3000", "http://newapi:3000/api", "http://newapi:3000?x", "newapi"])
def test_upstream_url_validated(value):
    with pytest.raises(SettingsError):
        load(PROXY_UPSTREAM_URL=value)


def test_secrets_set_for_redaction_holds_all_key_material():
    settings, _ = load()
    assert {WEB_SECRET, CRON_SECRET, AP_TOKEN, OP_TOKEN} <= settings.secrets


def test_valkey_required_with_more_than_one_replica():
    # A replay sent to the other replica must be caught: no shared record, no start.
    with pytest.raises(SettingsError) as info:
        load(PROXY_VALKEY_ADDR="", PROXY_REPLICAS="2")
    assert info.value.name == "PROXY_VALKEY_ADDR"
    settings, _ = load(PROXY_VALKEY_ADDR="", PROXY_VALKEY_PASSWORD="", PROXY_REPLICAS="1")
    assert settings.valkey_host is None


def test_valkey_settings_parsed():
    settings, _ = load()
    assert (settings.valkey_host, settings.valkey_port, settings.valkey_user) == ("newapi-valkey", 6379, "newapi-proxy")
    assert settings.valkey_password == VALKEY_PASSWORD
    assert VALKEY_PASSWORD in settings.secrets  # masked in every log line
    assert VALKEY_PASSWORD not in repr(settings)


@pytest.mark.parametrize(
    "overrides, name",
    [
        ({"PROXY_VALKEY_ADDR": "newapi-valkey"}, "PROXY_VALKEY_ADDR"),
        ({"PROXY_VALKEY_ADDR": "redis://newapi-valkey:6379"}, "PROXY_VALKEY_ADDR"),
        ({"PROXY_VALKEY_ADDR": "newapi-valkey:99999"}, "PROXY_VALKEY_ADDR"),
        ({"PROXY_VALKEY_PASSWORD": ""}, "PROXY_VALKEY_PASSWORD"),
        ({"PROXY_VALKEY_PASSWORD": "short"}, "PROXY_VALKEY_PASSWORD"),
        ({"PROXY_VALKEY_PASSWORD": VALKEY_PASSWORD + " x"}, "PROXY_VALKEY_PASSWORD"),
        ({"PROXY_VALKEY_USER": "bad user"}, "PROXY_VALKEY_USER"),
    ],
)
def test_bad_valkey_settings_fail_closed_without_value(overrides, name):
    with pytest.raises(SettingsError) as info:
        load(**overrides)
    assert info.value.name == name
    assert VALKEY_PASSWORD not in str(info.value)


def test_ports_and_limits():
    settings, _ = load()
    assert (settings.port, settings.admin_port, settings.max_connections) == (8080, 8081, 128)
    assert settings.max_response_bytes == 1024 * 1024
    with pytest.raises(SettingsError):
        load(PROXY_ADMIN_PORT="8080")
    with pytest.raises(SettingsError):
        load(PROXY_MAX_CONNECTIONS="1")
