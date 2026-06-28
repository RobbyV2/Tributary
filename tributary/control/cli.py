import argparse
import sys

from tributary.control.ipc import (
    ErrorResp,
    GraphReq,
    GraphResp,
    HeadphoneReq,
    ListReq,
    ListResp,
    MuteReq,
    OkResp,
    Request,
    Response,
    StatusReq,
    StatusResp,
    VolumeReq,
    request,
)

_TRUE = frozenset({"on", "true", "1", "yes"})
_FALSE = frozenset({"off", "false", "0", "no"})


def _bool(text: str) -> bool:
    low = text.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise argparse.ArgumentTypeError(f"expected bool, got {text!r}")


def _to_request(args: argparse.Namespace) -> Request:
    match args.cmd:
        case "list":
            return ListReq()
        case "volume":
            return VolumeReq(args.source, args.level)
        case "mute":
            return MuteReq(args.source, args.state)
        case "headphone":
            return HeadphoneReq(args.mac)
        case "status":
            return StatusReq()
        case "graph":
            return GraphReq()
        case other:
            raise SystemExit(f"unknown command {other!r}")


def _render(resp: Response) -> int:
    match resp:
        case ListResp(sources=sources):
            print(f"{'MAC':<18} {'NAME':<40} {'VOL':>5} MUTE")
            for s in sources:
                print(f"{s.mac or '-':<18} {(s.name or '-'):<40} {s.volume:>5.2f} {'yes' if s.muted else 'no'}")
        case StatusResp() as r:
            rows = [
                ("headphone", r.headphone_mac),
                ("connected", "yes" if r.connected else "no"),
                ("in_codec", r.in_codec or "-"),
                ("out_codec", r.out_codec or "-"),
                ("adapter_mode", r.adapter_mode),
                ("sink_hci", r.sink_hci or "-"),
                ("source_hci", r.source_hci or "-"),
                ("latency_ms", "unmeasured" if r.latency_ms is None else f"{r.latency_ms:.1f}"),
                ("host_audio", "on" if r.host_audio else "off"),
            ]
            for key, value in rows:
                print(f"{key:<14} {value}")
        case GraphResp(topology=topology):
            print(topology)
        case OkResp(detail=detail):
            print(detail)
        case ErrorResp(message=message):
            print(f"error: {message}", file=sys.stderr)
            return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trib")
    p.add_argument("--socket", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    v = sub.add_parser("volume")
    v.add_argument("source")
    v.add_argument("level", type=float)
    m = sub.add_parser("mute")
    m.add_argument("source")
    m.add_argument("state", type=_bool)
    h = sub.add_parser("headphone")
    h.add_argument("mac")
    sub.add_parser("status")
    sub.add_parser("graph")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    resp = request(_to_request(args), path=args.socket)
    return _render(resp)


if __name__ == "__main__":
    raise SystemExit(main())
