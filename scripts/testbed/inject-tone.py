#!/usr/bin/env python3
import argparse
import math
import struct
import sys
import wave

try:
    import numpy as np
except ImportError:
    np = None


def synth_np(args):
    n = int(args.duration * args.rate)
    t = np.arange(n) / args.rate
    if args.tone is not None:
        sig = 0.4 * np.sin(2 * np.pi * args.tone * t)
    else:
        k = (args.f1 / args.f0) ** (1.0 / args.duration)
        phase = 2 * np.pi * args.f0 * (k**t - 1.0) / math.log(k)
        sig = 0.4 * np.sin(phase)
        i = int(args.impulse_offset * args.rate)
        if 0 <= i < n:
            sig[i : i + max(1, int(args.rate * 0.0005))] = 0.95
    return (sig * 32767).astype("<i2")


def synth_std(args):
    n = int(args.duration * args.rate)
    out = bytearray()
    k = (args.f1 / args.f0) ** (1.0 / args.duration) if args.tone is None else 1.0
    lk = math.log(k) if k != 1.0 else 1.0
    iw = max(1, int(args.rate * 0.0005))
    istart = int(args.impulse_offset * args.rate)
    for j in range(n):
        tt = j / args.rate
        if args.tone is not None:
            s = 0.4 * math.sin(2 * math.pi * args.tone * tt)
        elif istart <= j < istart + iw:
            s = 0.95
        else:
            ph = 2 * math.pi * args.f0 * (k**tt - 1.0) / lk
            s = 0.4 * math.sin(ph)
        out += struct.pack("<h", int(s * 32767))
    return bytes(out)


def main():
    p = argparse.ArgumentParser(description="synthesize sweep+impulse or steady tone stimulus")
    p.add_argument("output")
    p.add_argument("--duration", type=float, default=10.0)
    p.add_argument("--rate", type=int, default=48000)
    p.add_argument("--tone", type=float, default=None, help="steady sine freq; omit for sweep")
    p.add_argument("--f0", type=float, default=300.0)
    p.add_argument("--f1", type=float, default=8000.0)
    p.add_argument("--impulse-offset", type=float, default=1.0)
    args = p.parse_args()

    if np is not None:
        mono = synth_np(args)
        frames = np.column_stack([mono, mono]).reshape(-1).tobytes()
    else:
        m = synth_std(args)
        frames = bytearray()
        for off in range(0, len(m), 2):
            frames += m[off : off + 2] * 2
        frames = bytes(frames)

    with wave.open(args.output, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(args.rate)
        w.writeframes(frames)

    mode = "tone" if args.tone is not None else "sweep"
    print(f"wrote {args.output} mode={mode} rate={args.rate} duration={args.duration}", file=sys.stderr)
    if args.tone is not None:
        print(f"tone_freq={args.tone}")
    else:
        print(f"impulse_offset_s={args.impulse_offset} f0={args.f0} f1={args.f1}")


if __name__ == "__main__":
    main()
