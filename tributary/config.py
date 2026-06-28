import difflib
import enum
import logging
import math
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import NewType

logger = logging.getLogger("tributary.config")

DEFAULT_SAMPLE_RATE = 48000
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 192000
DEFAULT_RECONCILE_INTERVAL = 1.5
MAX_GAIN = 4.0

Mac = NewType("Mac", str)

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_HCI_RE = re.compile(r"^hci\d+$")
_DROPOUT_MSG = "single-adapter mode: sink and source share one radio; expect audio dropouts"
_TOP_KEYS = frozenset(
    {"headphone_mac", "allow_macs", "allow_patterns", "sample_rate", "reconcile_interval", "single_adapter", "gains", "adapters", "include_host_audio", "host_source"}
)
_ADAPTER_KEYS = frozenset({"sink_adapter", "source_adapter"})


class ConfigError(Exception):
    pass


class ConfigFileError(ConfigError):
    def __init__(self, path: object, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"cannot read config {path!r}: {reason}")


class MissingFieldError(ConfigError):
    def __init__(self, field: str, source: str) -> None:
        self.field = field
        self.source = source
        super().__init__(f"{source}: missing required field {field!r}")


class InvalidValueError(ConfigError):
    def __init__(self, field: str, value: object, expected: str, source: str = "<config>") -> None:
        self.field = field
        self.value = value
        self.expected = expected
        self.source = source
        super().__init__(f"{source}: {field}={value!r} invalid; expected {expected}")


class UnknownKeyError(ConfigError):
    def __init__(self, key: str, scope: str, suggestion: str | None, source: str) -> None:
        self.key = key
        self.scope = scope
        self.suggestion = suggestion
        self.source = source
        hint = f"; did you mean {suggestion!r}?" if suggestion else ""
        super().__init__(f"{source}: unknown key {key!r} in {scope}{hint}")


class AdapterResolutionError(ConfigError):
    def __init__(self, requested: tuple[str, ...], available: tuple[str, ...]) -> None:
        self.requested = requested
        self.available = available
        super().__init__(f"cannot resolve adapters; requested {requested}, available {available}")


class AdapterMode(enum.Enum):
    SINGLE = "single"
    DUAL = "dual"


def parse_mac(value: object, *, field: str, source: str) -> Mac:
    if not isinstance(value, str) or not _MAC_RE.match(value):
        raise InvalidValueError(field, value, "MAC address AA:BB:CC:DD:EE:FF", source)
    return Mac(value.upper())


@dataclass(frozen=True, slots=True)
class SourceAllowlist:
    macs: tuple[Mac, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()

    def admits(self, mac: Mac, node_name: str) -> bool:
        return mac in self.macs or any(p.search(node_name) for p in self.patterns)


@dataclass(frozen=True, slots=True)
class AdapterMap:
    sink_adapter: str
    source_adapter: str


@dataclass(frozen=True, slots=True)
class ResolvedAdapters:
    sink_adapter: str
    source_adapter: str
    single: bool


def _check_adapter_combo(adapters: AdapterMap | None, single_adapter: bool, *, source: str) -> None:
    match adapters:
        case None:
            return
        case AdapterMap(sink_adapter=s, source_adapter=t):
            if s == t and not single_adapter:
                raise InvalidValueError(
                    "adapters", s, "same adapter named for both roles; set single_adapter=true or use distinct adapters", source
                )
            if s != t and single_adapter:
                raise InvalidValueError(
                    "single_adapter", single_adapter, "distinct adapters named; omit single_adapter or name one adapter for both roles", source
                )


def _adapter_warnings(adapters: AdapterMap | None, single_adapter: bool) -> tuple[str, ...]:
    match adapters:
        case None:
            return (_DROPOUT_MSG, "single_adapter set but ignored; [adapters] table omitted") if single_adapter else (_DROPOUT_MSG,)
        case AdapterMap(sink_adapter=s, source_adapter=t) if s == t:
            return (_DROPOUT_MSG,)
        case _:
            return ()


@dataclass(frozen=True, slots=True)
class Config:
    headphone_mac: Mac
    allowlist: SourceAllowlist = field(default_factory=SourceAllowlist)
    gains: Mapping[Mac, float] = field(default_factory=lambda: MappingProxyType({}))
    sample_rate: int = DEFAULT_SAMPLE_RATE
    reconcile_interval: float = DEFAULT_RECONCILE_INTERVAL
    single_adapter: bool = False
    adapters: AdapterMap | None = None
    include_host_audio: bool = False
    host_source: str | None = None

    def __post_init__(self) -> None:
        _check_adapter_combo(self.adapters, self.single_adapter, source="<config>")
        if isinstance(self.gains, dict):
            object.__setattr__(self, "gains", MappingProxyType(dict(self.gains)))

    @property
    def adapter_mode(self) -> AdapterMode:
        match self.adapters:
            case None:
                return AdapterMode.SINGLE
            case AdapterMap(sink_adapter=s, source_adapter=t):
                return AdapterMode.SINGLE if s == t else AdapterMode.DUAL

    def resolve_adapters(self, available: Sequence[str]) -> ResolvedAdapters:
        ordered = sorted(available, key=lambda n: int(n[3:]))
        match self.adapters:
            case None:
                if not ordered:
                    raise AdapterResolutionError((), tuple(available))
                return ResolvedAdapters(ordered[0], ordered[0], True)
            case AdapterMap(sink_adapter=s, source_adapter=t) if s == t:
                if s not in ordered:
                    raise AdapterResolutionError((s,), tuple(available))
                return ResolvedAdapters(s, s, True)
            case AdapterMap(sink_adapter=s, source_adapter=t):
                missing = tuple(n for n in (s, t) if n not in ordered)
                if missing:
                    raise AdapterResolutionError(missing, tuple(available))
                return ResolvedAdapters(s, t, False)


def _reject_unknown(table: Mapping[str, object], known: frozenset[str], scope: str, source: str) -> None:
    for key in table:
        if key not in known:
            matches = difflib.get_close_matches(key, list(known), n=1, cutoff=0.6)
            raise UnknownKeyError(key, scope, matches[0] if matches else None, source)


def _parse_sample_rate(value: object, source: str) -> int:
    if type(value) is not int or not (MIN_SAMPLE_RATE <= value <= MAX_SAMPLE_RATE):
        raise InvalidValueError("sample_rate", value, f"int in [{MIN_SAMPLE_RATE}, {MAX_SAMPLE_RATE}]", source)
    return value


def _parse_interval(value: object, source: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise InvalidValueError("reconcile_interval", value, "finite number > 0", source)
    return float(value)


def _parse_gain(value: object, key: str, source: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not (0.0 <= value <= MAX_GAIN):
        raise InvalidValueError(f"gains.{key}", value, f"finite number in [0.0, {MAX_GAIN}]", source)
    return float(value)


def _parse_gains(raw: Mapping[str, object], source: str) -> MappingProxyType[Mac, float]:
    out: dict[Mac, float] = {}
    for key, value in raw.items():
        out[parse_mac(key, field=f"gains.{key}", source=source)] = _parse_gain(value, key, source)
    return MappingProxyType(out)


def _compile_pattern(value: object, index: int, source: str) -> re.Pattern[str]:
    field = f"allow_patterns[{index}]"
    if not isinstance(value, str):
        raise InvalidValueError(field, value, "regex string", source)
    try:
        return re.compile(value)
    except re.error as e:
        raise InvalidValueError(field, value, f"valid regex ({e})", source) from e


def _require_hci(table: Mapping[str, object], key: str, source: str) -> str:
    if key not in table:
        raise MissingFieldError(f"adapters.{key}", source)
    value = table[key]
    if not isinstance(value, str) or not _HCI_RE.match(value):
        raise InvalidValueError(f"adapters.{key}", value, "adapter name like hci0", source)
    return value


def _parse_adapters(value: object, source: str) -> AdapterMap | None:
    match value:
        case None:
            return None
        case Mapping():
            _reject_unknown(value, _ADAPTER_KEYS, "adapters", source)
            return AdapterMap(_require_hci(value, "sink_adapter", source), _require_hci(value, "source_adapter", source))
        case _:
            raise InvalidValueError("adapters", value, "table with sink_adapter and source_adapter", source)


def loads(text: str, *, source: str = "<string>") -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigFileError(source, str(e)) from e
    _reject_unknown(raw, _TOP_KEYS, "top-level", source)
    if "headphone_mac" not in raw:
        raise MissingFieldError("headphone_mac", source)
    adapters = _parse_adapters(raw.get("adapters"), source)
    single_adapter = raw.get("single_adapter", False)
    if type(single_adapter) is not bool:
        raise InvalidValueError("single_adapter", single_adapter, "boolean", source)
    include_host_audio = raw.get("include_host_audio", False)
    if type(include_host_audio) is not bool:
        raise InvalidValueError("include_host_audio", include_host_audio, "boolean", source)
    host_source = raw.get("host_source")
    if host_source is not None and not isinstance(host_source, str):
        raise InvalidValueError("host_source", host_source, "string node name or omitted", source)
    config = Config(
        headphone_mac=parse_mac(raw["headphone_mac"], field="headphone_mac", source=source),
        allowlist=SourceAllowlist(
            macs=tuple(parse_mac(v, field=f"allow_macs[{i}]", source=source) for i, v in enumerate(raw.get("allow_macs", ()))),
            patterns=tuple(_compile_pattern(v, i, source) for i, v in enumerate(raw.get("allow_patterns", ()))),
        ),
        gains=_parse_gains(raw.get("gains", {}), source),
        sample_rate=_parse_sample_rate(raw.get("sample_rate", DEFAULT_SAMPLE_RATE), source),
        reconcile_interval=_parse_interval(raw.get("reconcile_interval", DEFAULT_RECONCILE_INTERVAL), source),
        single_adapter=single_adapter,
        adapters=adapters,
        include_host_audio=include_host_audio,
        host_source=host_source,
    )
    for message in _adapter_warnings(adapters, single_adapter):
        logger.warning(message)
    return config


def load_config(path: str | os.PathLike[str]) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigFileError(path, str(e)) from e
    return loads(text, source=str(path))
