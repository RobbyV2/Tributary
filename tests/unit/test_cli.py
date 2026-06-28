import pytest

from tributary.control import cli
from tributary.control.ipc import (
    ErrorResp,
    GraphReq,
    GraphResp,
    HeadphoneReq,
    ListReq,
    ListResp,
    MuteReq,
    OkResp,
    SourceInfo,
    StatusReq,
    StatusResp,
    VolumeReq,
)


def run_main(monkeypatch, argv, resp):
    captured: dict[str, object] = {}

    def fake_request(req, *, path=None):
        captured["req"] = req
        captured["path"] = path
        return resp

    monkeypatch.setattr(cli, "request", fake_request)
    code = cli.main(argv)
    return code, captured


def test_list_request_and_render(monkeypatch, capsys):
    resp = ListResp((SourceInfo("AA:BB:CC:DD:EE:FF", "bluez_input.x", 95, 0.56, False),))
    code, captured = run_main(monkeypatch, ["list"], resp)
    assert code == 0
    assert captured["req"] == ListReq()
    out = capsys.readouterr().out
    assert "AA:BB:CC:DD:EE:FF" in out
    assert "0.56" in out


def test_volume_request(monkeypatch, capsys):
    code, captured = run_main(monkeypatch, ["volume", "phone", "0.3"], OkResp("ok"))
    assert code == 0
    assert captured["req"] == VolumeReq("phone", 0.3)


def test_mute_request(monkeypatch, capsys):
    _, captured = run_main(monkeypatch, ["mute", "phone", "on"], OkResp("ok"))
    assert captured["req"] == MuteReq("phone", True)


def test_headphone_request(monkeypatch, capsys):
    _, captured = run_main(monkeypatch, ["headphone", "AA:BB:CC:DD:EE:FF"], OkResp("ok"))
    assert captured["req"] == HeadphoneReq("AA:BB:CC:DD:EE:FF")


def test_status_render_unmeasured(monkeypatch, capsys):
    resp = StatusResp("AA:BB:CC:DD:EE:FF", True, None, "aac", "single", None, None, None, False)
    code, captured = run_main(monkeypatch, ["status"], resp)
    assert code == 0
    assert captured["req"] == StatusReq()
    out = capsys.readouterr().out
    assert "unmeasured" in out
    assert "aac" in out


def test_graph_render(monkeypatch, capsys):
    code, captured = run_main(monkeypatch, ["graph"], GraphResp("a:p -> b:q"))
    assert code == 0
    assert captured["req"] == GraphReq()
    assert "a:p -> b:q" in capsys.readouterr().out


def test_error_exit_code(monkeypatch, capsys):
    code, _ = run_main(monkeypatch, ["list"], ErrorResp("boom"))
    assert code == 1
    assert "boom" in capsys.readouterr().err


def test_socket_flag_passed(monkeypatch, capsys):
    _, captured = run_main(monkeypatch, ["--socket", "/x.sock", "status"],
                           StatusResp("m", False, None, None, "single", None, None, None, False))
    assert captured["path"] == "/x.sock"
