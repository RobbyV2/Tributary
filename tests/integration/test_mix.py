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
from tributary.config import DEFAULT_SAMPLE_RATE

MIX = "tributary_mix"
TONE_A = "trib_tone_a"
TONE_B = "trib_tone_b"
FREQ_A = 440.0
FREQ_B = 660.0
TEMP = Path("temp")


def _need(*bins: str) -> None:
    missing = [b for b in bins if shutil.which(b) is None]
    if missing:
        pytest.skip(f"missing PipeWire tools: {missing}")


def _tone(path: Path, freq: float, secs: float, rate: int) -> None:
    t = np.arange(int(secs * rate)) / rate
    wave_data = (0.4 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    stereo = np.column_stack([wave_data, wave_data]).reshape(-1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(stereo.tobytes())


def _play(path: Path, target: str, rate: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["pw-cat", "-p", "--target", target, "--rate", str(rate), "--channels", "2", "--format", "s16", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _peak(spectrum: np.ndarray, freqs: np.ndarray, target: float, tol: float) -> tuple[float, float]:
    band = np.abs(freqs - target) <= tol
    idx = np.argmax(np.where(band, spectrum, 0.0))
    return float(freqs[idx]), float(spectrum[idx])


@pytest.mark.integration
def test_mix_sums_two_links() -> None:
    _need("pw-dump", "pw-cat", "pw-record", "pw-link", "pw-cli")
    rate = DEFAULT_SAMPLE_RATE
    TEMP.mkdir(exist_ok=True)
    wav_a, wav_b, cap = TEMP / "tone_a.wav", TEMP / "tone_b.wav", TEMP / "mix_capture.wav"
    _tone(wav_a, FREQ_A, 20.0, rate)
    _tone(wav_b, FREQ_B, 20.0, rate)

    procs: list[subprocess.Popen[bytes]] = []
    try:
        mix_node = mixer.ensure_bus(MIX)
        tone_a = pw.create_null_sink(TONE_A)
        tone_b = pw.create_null_sink(TONE_B)

        procs.append(_play(wav_a, TONE_A, rate))
        procs.append(_play(wav_b, TONE_B, rate))
        time.sleep(1.5)

        graph = pw.dump()
        delta = router.reconcile(graph, mix_node, (tone_a, tone_b), run=pw._run)
        assert len(delta.to_add) == 4, delta
        assert len(delta.to_remove) == 0, delta

        after = pw.dump()
        assert len(router.actual_source_links(after, mix_node, (tone_a, tone_b))) == 4

        rec = subprocess.Popen(
            ["pw-record", "-P", "stream.capture.sink=true", "--target", MIX,
             "--rate", str(rate), "--channels", "2", "--format", "s16", str(cap)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(rec)
        time.sleep(4.0)
        rec.send_signal(signal.SIGINT)
        rec.wait(timeout=10)

        with wave.open(str(cap), "rb") as w:
            frames = w.readframes(w.getnframes())
            ch = w.getnchannels()
        data = np.frombuffer(frames, dtype="<i2").astype(np.float64)
        mono = data.reshape(-1, ch).mean(axis=1)[int(0.5 * rate):]
        mono = mono - mono.mean()
        rms = float(np.sqrt(np.mean(mono**2)))
        assert rms > 0.0

        spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
        freqs = np.fft.rfftfreq(len(mono), 1.0 / rate)
        noise = float(np.median(spectrum[freqs > 100.0]))
        fa, ma = _peak(spectrum, freqs, FREQ_A, 15.0)
        fb, mb = _peak(spectrum, freqs, FREQ_B, 15.0)

        print(f"RMS={rms:.2f} peakA={fa:.1f}Hz mag={ma:.3e} peakB={fb:.1f}Hz mag={mb:.3e} noise={noise:.3e}")
        assert ma > 30 * noise, (ma, noise)
        assert mb > 30 * noise, (mb, noise)
        assert abs(fa - FREQ_A) <= 15.0
        assert abs(fb - FREQ_B) <= 15.0
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        mixer.teardown(MIX)
        pw.destroy_null_sink(TONE_A)
        pw.destroy_null_sink(TONE_B)
        names = {n.name for n in pw.dump().nodes}
        assert not (names & {MIX, TONE_A, TONE_B}), names
