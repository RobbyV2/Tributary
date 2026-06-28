import dataclasses
import logging
import re
import tomllib
from pathlib import Path
from types import MappingProxyType

import pytest

from tributary.config import (
    AdapterMap,
    AdapterMode,
    AdapterResolutionError,
    Config,
    ConfigFileError,
    InvalidValueError,
    Mac,
    MissingFieldError,
    SourceAllowlist,
    UnknownKeyError,
    load_config,
    loads,
    parse_mac,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "config" / "tributary.example.toml"
HEAD = 'headphone_mac = "B4:23:A2:01:6D:27"\n'
HEADPHONE = Mac("B4:23:A2:01:6D:27")
LISTED = Mac("E4:5F:01:E6:31:85")


def load(body: str = "") -> Config:
    return loads(HEAD + body)


def test_loads_minimal_normalizes_mac() -> None:
    c = loads('headphone_mac = "b4:23:a2:01:6d:27"')
    assert c.headphone_mac == "B4:23:A2:01:6D:27"
    assert c.sample_rate == 48000
    assert c.reconcile_interval == 1.5
    assert c.single_adapter is False
    assert c.adapters is None
    assert c.adapter_mode is AdapterMode.SINGLE


def test_parse_mac_rejects_non_colon() -> None:
    with pytest.raises(InvalidValueError):
        parse_mac("B4-23-A2-01-6D-27", field="x", source="<t>")
    with pytest.raises(InvalidValueError):
        parse_mac(123, field="x", source="<t>")


def test_gains_read_only() -> None:
    c = load('[gains]\n"E4:5F:01:E6:31:85" = 1.0\n')
    assert c.gains[LISTED] == 1.0
    with pytest.raises(TypeError):
        c.gains["x"] = 2.0  # type: ignore[index]


def test_direct_config_wraps_dict_gains() -> None:
    c = Config(headphone_mac=HEADPHONE, gains={LISTED: 1.0})
    assert isinstance(c.gains, MappingProxyType)
    with pytest.raises(TypeError):
        c.gains["y"] = 1.0  # type: ignore[index]


def test_default_gains_read_only() -> None:
    with pytest.raises(TypeError):
        Config(headphone_mac=HEADPHONE).gains["z"] = 1.0  # type: ignore[index]


def test_example_loads() -> None:
    c = load_config(EXAMPLE)
    assert c.headphone_mac == "B4:23:A2:01:6D:27"
    assert c.allowlist.macs == (LISTED,)
    assert c.allowlist.patterns == ()
    assert c.sample_rate == 48000
    assert c.reconcile_interval == 1.5
    assert c.single_adapter is False
    assert c.adapters is None
    assert c.adapter_mode is AdapterMode.SINGLE
    assert c.gains[LISTED] == 1.0


def test_admits_union() -> None:
    other = Mac("AA:BB:CC:DD:EE:FF")
    macs_only = SourceAllowlist(macs=(LISTED,))
    assert macs_only.admits(LISTED, "anything")
    assert not macs_only.admits(other, "anything")
    pat_only = SourceAllowlist(patterns=(re.compile(r"^bluez_input\.E4_5F"),))
    assert pat_only.admits(other, "bluez_input.E4_5F_01_E6_31_85")
    assert not pat_only.admits(other, "bluez_input.other")
    assert not SourceAllowlist().admits(LISTED, "bluez_input.whatever")


def test_example_allowlist_fail_closed() -> None:
    c = load_config(EXAMPLE)
    other = Mac("11:22:33:44:55:66")
    assert c.allowlist.admits(LISTED, "bluez_input.E4_5F")
    assert not c.allowlist.admits(other, "bluez_input.anything")


def test_single_adapter_ignored_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="tributary.config"):
        c = load("single_adapter = true\n")
    assert c.adapters is None
    assert c.adapter_mode is AdapterMode.SINGLE
    assert any("ignored" in r.message for r in caplog.records)


@pytest.mark.parametrize("rate", [32000, 176400])
def test_sample_rate_accept(rate: int) -> None:
    assert load(f"sample_rate = {rate}\n").sample_rate == rate


@pytest.mark.parametrize("body", ["sample_rate = 7999", "sample_rate = 192001", "sample_rate = 44100.0", "sample_rate = true"])
def test_sample_rate_reject(body: str) -> None:
    with pytest.raises(InvalidValueError):
        load(body + "\n")


def test_adapter_mode_property() -> None:
    assert Config(headphone_mac=HEADPHONE).adapter_mode is AdapterMode.SINGLE
    assert Config(headphone_mac=HEADPHONE, single_adapter=True, adapters=AdapterMap("hci0", "hci0")).adapter_mode is AdapterMode.SINGLE
    assert Config(headphone_mac=HEADPHONE, adapters=AdapterMap("hci0", "hci1")).adapter_mode is AdapterMode.DUAL


def test_case3_distinct_single_raises() -> None:
    with pytest.raises(InvalidValueError):
        Config(headphone_mac=HEADPHONE, single_adapter=True, adapters=AdapterMap("hci0", "hci1"))


def test_case5_equal_nonsingle_raises() -> None:
    with pytest.raises(InvalidValueError):
        Config(headphone_mac=HEADPHONE, adapters=AdapterMap("hci0", "hci0"))


def test_adapter_mode_not_a_field() -> None:
    assert "adapter_mode" not in {f.name for f in dataclasses.fields(Config)}


def test_frozen_field_set() -> None:
    c = Config(headphone_mac=HEADPHONE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.sample_rate = 44100  # type: ignore[misc]


def test_m1_asymmetry(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="tributary.config"):
        c = load("")
    assert c.adapter_mode is AdapterMode.SINGLE
    assert any("dropout" in r.message.lower() for r in caplog.records)
    with pytest.raises(InvalidValueError):
        load('[adapters]\nsink_adapter = "hci0"\nsource_adapter = "hci0"\n')


def test_resolve_single_unnamed_numeric_sort() -> None:
    r = Config(headphone_mac=HEADPHONE).resolve_adapters(["hci10", "hci2"])
    assert (r.sink_adapter, r.source_adapter, r.single) == ("hci2", "hci2", True)


def test_resolve_single_unnamed_empty() -> None:
    with pytest.raises(AdapterResolutionError):
        Config(headphone_mac=HEADPHONE).resolve_adapters([])


def test_resolve_single_named() -> None:
    c = Config(headphone_mac=HEADPHONE, single_adapter=True, adapters=AdapterMap("hci0", "hci0"))
    r = c.resolve_adapters(["hci0", "hci1"])
    assert (r.sink_adapter, r.source_adapter, r.single) == ("hci0", "hci0", True)
    with pytest.raises(AdapterResolutionError):
        c.resolve_adapters(["hci1"])


def test_resolve_dual() -> None:
    c = Config(headphone_mac=HEADPHONE, adapters=AdapterMap("hci0", "hci1"))
    r = c.resolve_adapters(["hci1", "hci0"])
    assert (r.sink_adapter, r.source_adapter, r.single) == ("hci0", "hci1", False)
    with pytest.raises(AdapterResolutionError):
        c.resolve_adapters(["hci0"])


def test_gain_key_normalized() -> None:
    c = load('[gains]\n"e4:5f:01:e6:31:85" = 0.5\n')
    assert c.gains[LISTED] == 0.5


def test_toml_parse_error_chained() -> None:
    with pytest.raises(ConfigFileError) as ei:
        loads("headphone_mac = bar\n")
    assert "line" in ei.value.reason
    assert isinstance(ei.value.__cause__, tomllib.TOMLDecodeError)


def test_gains_arbitrary_macs() -> None:
    c = load('[gains]\n"AA:BB:CC:DD:EE:FF" = 2.0\n"11:22:33:44:55:66" = 0.0\n')
    assert c.gains[Mac("AA:BB:CC:DD:EE:FF")] == 2.0
    assert c.gains[Mac("11:22:33:44:55:66")] == 0.0


def test_gains_non_mac_key() -> None:
    with pytest.raises(InvalidValueError):
        load('[gains]\n"not-a-mac" = 1.0\n')


def test_top_level_typo_suggests() -> None:
    with pytest.raises(UnknownKeyError) as ei:
        loads('headphon_mac = "B4:23:A2:01:6D:27"')
    assert ei.value.suggestion == "headphone_mac"


def test_missing_headphone_mac() -> None:
    with pytest.raises(MissingFieldError):
        loads("sample_rate = 48000")


def test_bad_regex() -> None:
    with pytest.raises(InvalidValueError):
        load('allow_patterns = ["("]\n')


@pytest.mark.parametrize(
    "body",
    [
        '[gains]\n"AA:BB:CC:DD:EE:FF" = 5.0\n',
        '[gains]\n"AA:BB:CC:DD:EE:FF" = -0.1\n',
        '[gains]\n"AA:BB:CC:DD:EE:FF" = true\n',
        "reconcile_interval = 0\n",
        "reconcile_interval = -1.0\n",
        "reconcile_interval = true\n",
        'single_adapter = "yes"\n',
        "single_adapter = 1\n",
    ],
)
def test_bad_values_rejected(body: str) -> None:
    with pytest.raises(InvalidValueError):
        load(body)


def test_host_audio_defaults() -> None:
    c = load("")
    assert c.include_host_audio is False
    assert c.host_source is None


def test_host_audio_parsed() -> None:
    c = load('include_host_audio = true\nhost_source = "alsa_output.pci.analog-stereo"\n')
    assert c.include_host_audio is True
    assert c.host_source == "alsa_output.pci.analog-stereo"


def test_host_audio_default_source_none() -> None:
    c = load("include_host_audio = true\n")
    assert c.include_host_audio is True
    assert c.host_source is None


@pytest.mark.parametrize("body", ['include_host_audio = "yes"\n', "include_host_audio = 1\n", "host_source = 5\n", "host_source = true\n"])
def test_host_audio_reject(body: str) -> None:
    with pytest.raises(InvalidValueError):
        load(body)


def test_single_adapter_bool_accepted() -> None:
    assert load("single_adapter = false\n").single_adapter is False
    assert load("single_adapter = true\n").single_adapter is True


def test_bad_hci_name() -> None:
    with pytest.raises(InvalidValueError):
        load('[adapters]\nsink_adapter = "wlan0"\nsource_adapter = "hci1"\n')


def test_adapters_missing_role() -> None:
    with pytest.raises(MissingFieldError):
        load('[adapters]\nsink_adapter = "hci0"\n')
