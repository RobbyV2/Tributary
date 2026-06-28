import os
import re
import shutil
import signal
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from tributary.audio import mixer
from tributary.audio import pipewire as pw
from tributary.audio import router
from tributary.bluetooth.headphones import find_sink_node
from tributary.config import DEFAULT_SAMPLE_RATE, Mac, SourceAllowlist

pytestmark = pytest.mark.skipif(
    not os.environ.get("TRIBUTARY_TEST_HOST"),
    reason="set TRIBUTARY_TEST_HOST to run BT smoke tests",
)

REPO = next(p for p in Path(__file__).resolve().parents if (p / ".claude" / "pi-ssh.sh").exists())
WRAPPER = REPO / ".claude" / "pi-ssh.sh"
TESTBED = REPO / "scripts" / "testbed"
REMOTE_DIR = "/tmp/trib-testbed"
TEMP = REPO / "temp"
MIX = "tributary_mix"
RATE = DEFAULT_SAMPLE_RATE
PHONE_FREQ = 440.0
IMPULSE_OFFSET = 2.0
LEAD_S = 1.5
LATENCY_CEILING_MS = 1000.0


def _pi(cmd: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(WRAPPER), cmd], capture_output=True, text=True, timeout=timeout)


def _pi_popen(cmd: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([str(WRAPPER), cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _pi_scp(src: Path, dst: str) -> None:
    r = subprocess.run([str(WRAPPER), "scp", str(src), dst], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        pytest.skip(f"remote unreachable (scp {src.name}): {r.stderr.strip()}")


def _need(*bins: str) -> None:
    missing = [b for b in bins if shutil.which(b) is None]
    if missing:
        pytest.skip(f"missing local tools: {missing}")


def _controller_mac(run) -> str:
    out = run().stdout
    m = re.search(r"Controller\s+([0-9A-Fa-f:]{17})", out)
    if m is None:
        pytest.skip("could not read Controller MAC")
    return m.group(1).upper()


def _host_mac() -> str:
    return _controller_mac(lambda: subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, timeout=10))


def _remote_mac() -> str:
    r = _pi("bluetoothctl show", timeout=20)
    if r.returncode != 0:
        pytest.skip(f"remote unreachable (bluetoothctl show): {r.stderr.strip()}")
    return _controller_mac(lambda: r)


def adapter_mode() -> str:
    base = Path("/sys/class/bluetooth")
    n = len([d for d in base.iterdir() if d.name.startswith("hci")]) if base.exists() else 0
    return "dual" if n >= 2 else "single"


@pytest.fixture(scope="module")
def push_scripts() -> None:
    _pi(f"mkdir -p {REMOTE_DIR}", timeout=20)
    for name in ("inject-tone.py", "fake-phone.sh", "fake-headphones.sh"):
        _pi_scp(TESTBED / name, f"{REMOTE_DIR}/{name}")
    _pi(f"chmod +x {REMOTE_DIR}/*.sh {REMOTE_DIR}/inject-tone.py", timeout=20)


def _load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        ch, rate = w.getnchannels(), w.getframerate()
        frames = w.readframes(w.getnframes())
    data = np.frombuffer(frames, dtype="<i2").astype(np.float64)
    if data.size == 0:
        return data, rate
    return data.reshape(-1, ch).mean(axis=1), rate


def _peak(mono: np.ndarray, rate: int, target: float, tol: float) -> tuple[float, float, float]:
    mono = mono - mono.mean()
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
    freqs = np.fft.rfftfreq(len(mono), 1.0 / rate)
    band = np.abs(freqs - target) <= tol
    idx = int(np.argmax(np.where(band, spectrum, 0.0)))
    noise = float(np.median(spectrum[freqs > 100.0]))
    return float(freqs[idx]), float(spectrum[idx]), noise


def _dropout_count(mono: np.ndarray, rate: int) -> int:
    win = max(1, int(rate * 0.02))
    trimmed = mono[: len(mono) - len(mono) % win]
    if trimmed.size == 0:
        return 0
    env = np.sqrt((trimmed.reshape(-1, win) ** 2).mean(axis=1))
    thr = 0.2 * float(np.median(env))
    silent = env < thr
    return int(np.sum(silent[1:] & ~silent[:-1]))


def _reconcile_until_linked(mix_node, allowlist, deadline: float):
    sources: tuple = ()
    while time.time() < deadline:
        graph = pw.dump()
        sources = router.select_bluez_sources(graph, allowlist)
        if sources:
            router.reconcile(graph, mix_node, sources, run=pw._run)
            after = pw.dump()
            if router.actual_source_links(after, mix_node, sources):
                return sources, after
        time.sleep(0.5)
    return sources, pw.dump()


def _kill(*procs: subprocess.Popen) -> None:
    for p in procs:
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def test_fake_phone_tone_reaches_mix(push_scripts) -> None:
    _need("pw-dump", "pw-record", "pw-link", "pw-cli", "bluetoothctl")
    print(f"adapter_mode={adapter_mode()}")
    TEMP.mkdir(exist_ok=True)
    host_mac = _host_mac()
    cap = TEMP / "phone_mix_capture.wav"
    allowlist = SourceAllowlist(patterns=(re.compile(r"bluez_input"),))

    mix_node = mixer.ensure_bus(MIX)
    phone = _pi_popen(f"bash {REMOTE_DIR}/fake-phone.sh {host_mac} {int(PHONE_FREQ)} 30")
    rec = None
    try:
        sources, graph = _reconcile_until_linked(mix_node, allowlist, time.time() + 40.0)
        assert sources, "no bluez_input source node appeared on host"
        assert router.actual_source_links(graph, mix_node, sources), "reconciler did not link source into mix"

        rec = subprocess.Popen(
            ["pw-record", "-P", "stream.capture.sink=true", "--target", MIX,
             "--rate", str(RATE), "--channels", "2", "--format", "s16", str(cap)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(5.0)
        rec.send_signal(signal.SIGINT)
        rec.wait(timeout=10)

        mono, rate = _load_wav(cap)
        assert mono.size > rate, "mix capture empty or too short"
        mono = mono[int(0.5 * rate):]
        rms = float(np.sqrt(np.mean((mono - mono.mean()) ** 2)))
        fpk, mpk, noise = _peak(mono, rate, PHONE_FREQ, 15.0)
        print(f"phone: rms={rms:.1f} peak={fpk:.1f}Hz mag={mpk:.3e} noise={noise:.3e}")
        assert rms > 0.0
        assert mpk > 20 * noise, (mpk, noise)
        assert abs(fpk - PHONE_FREQ) <= 15.0
    finally:
        _kill(phone, *( [rec] if rec else [] ))
        _pi(f"bluetoothctl disconnect {host_mac}", timeout=15)
        mixer.teardown(MIX)


def test_reverse_leg_latency_and_dropouts(push_scripts) -> None:
    _need("pw-dump", "pw-cat", "pw-link", "pw-cli", "bluetoothctl", "python3")
    print(f"adapter_mode={adapter_mode()} (single-adapter is the supported default)")
    TEMP.mkdir(exist_ok=True)
    remote_mac = _remote_mac()
    stim = TEMP / "reverse_stim.wav"
    captured = TEMP / "reverse_capture.wav"
    dur = 12

    subprocess.run(
        ["python3", str(TESTBED / "inject-tone.py"), str(stim),
         "--duration", str(dur - 4), "--rate", str(RATE), "--impulse-offset", str(IMPULSE_OFFSET)],
        check=True, capture_output=True, text=True, timeout=60,
    )

    mix_node = mixer.ensure_bus(MIX)
    mx = mixer.Mixer(headphone_mac=remote_mac, sink_name=MIX, run=pw._run)
    play = None
    try:
        subprocess.run(["bluetoothctl", "--timeout", "5", "power", "on"], capture_output=True, text=True, timeout=15)
        rec = _pi_popen(f"bash {REMOTE_DIR}/fake-headphones.sh {dur} {REMOTE_DIR}/cap.wav")
        time.sleep(2.0)
        for cmd in (["pair", remote_mac], ["trust", remote_mac], ["connect", remote_mac]):
            subprocess.run(["bluetoothctl", "--timeout", "20", *cmd], capture_output=True, text=True, timeout=25)

        deadline = time.time() + 20.0
        hp = None
        while time.time() < deadline:
            hp = find_sink_node(pw.dump(), Mac(remote_mac))
            if hp is not None:
                break
            time.sleep(0.5)
        if hp is None:
            _kill(rec)
            pytest.skip(f"host did not see remote {remote_mac} as a2dp_source sink")
        mx.ensure_headphone_link()

        time.sleep(LEAD_S)
        play = subprocess.Popen(
            ["pw-cat", "-p", "--target", MIX, "--rate", str(RATE), "--channels", "2", "--format", "s16", str(stim)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        play.wait(timeout=dur + 10)
        rec.wait(timeout=dur + 15)
    finally:
        _kill(*( [play] if play else [] ))
        subprocess.run(["bluetoothctl", "--timeout", "10", "disconnect", remote_mac], capture_output=True, text=True, timeout=15)
        mixer.teardown(MIX)

    _fetch_remote(f"{REMOTE_DIR}/cap.wav", captured)
    mono, rate = _load_wav(captured)
    if mono.size == 0:
        pytest.fail("reverse-leg capture is empty; nothing arrived at the remote sink")

    impulse_sample = int(np.argmax(np.abs(mono)))
    arrival_s = impulse_sample / rate
    latency_ms = (arrival_s - (LEAD_S + IMPULSE_OFFSET)) * 1000.0
    dropouts = _dropout_count(mono, rate)
    print(f"reverse-leg: arrival={arrival_s:.3f}s latency={latency_ms:.1f}ms dropouts={dropouts}")

    assert np.isfinite(latency_ms), "no latency measurement produced"
    assert latency_ms < LATENCY_CEILING_MS, f"latency {latency_ms:.1f}ms exceeds ceiling"
    assert latency_ms > -200.0, f"implausible negative latency {latency_ms:.1f}ms (clock/sync issue)"


def _fetch_remote(remote_path: str, local: Path) -> None:
    import base64

    r = _pi(f"base64 {remote_path}", timeout=120)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.fail(f"could not retrieve remote capture {remote_path}: {r.stderr.strip()}")
    local.write_bytes(base64.b64decode(r.stdout))
