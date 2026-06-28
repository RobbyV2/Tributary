# Remote test host inventory: pi@kpi.local

Captured READ-ONLY via `.claude/pi-ssh.sh`. Remote reachable: YES (every wrapper
call succeeded). Raw outputs in the sibling `remote-*.txt` files.

## Hardware notes

Treating the remote as a second Linux box with its own radio that can play A2DP source
and sink roles for end-to-end tests, the captures show:

- Core assumption HOLDS. The remote is a Debian 12 (arm64, Raspberry Pi) Linux
  box with its own on-board Bluetooth radio (hci0), and BlueZ advertises BOTH the
  A2DP "Audio Source" (0x110a) and "Audio Sink" (0x110b) UUIDs, with PipeWire's
  libspa-bluez5 implementing both roles. So it can be fake-phone OR fake-headphones.

- SINGLE RADIO, ONE ROLE AT A TIME. There is exactly one controller (hci0,
  on-board Cypress/Infineon combo chip on UART; no USB BT dongle). A single
  adapter cannot be A2DP source and sink simultaneously, so the remote cannot
  self-loop source -> sink on its own. End-to-end tests must pair it with the
  other endpoint (the local DUT running Tributary); the remote acts as the peer to
  the local machine, not as a self-contained source->sink chain.

- STACK IS PIPEWIRE, NOT PULSEAUDIO OR BLUEALSA. PulseAudio 16.1 is installed but
  INACTIVE; the live server is PipeWire 1.2.7 + WirePlumber 0.4.13 with
  pipewire-pulse providing the PulseAudio socket. BlueALSA is absent. Steps written
  against `pactl load-module module-bluetooth-discover` on a real PulseAudio daemon,
  or against bluealsa, do not match this host; use the PipeWire path (pactl still
  works through the pipewire-pulse shim).

- AAC CODEC ABSENT. The PipeWire bluez5 codec set has SBC, aptX/aptX-HD, LDAC,
  LC3, Opus, FastStream, but NO AAC plugin (no libspa-codec-bluez5-aac.so, no
  libfdk-aac). libfaad present is only an AAC decoder, not an A2DP encode path.
  AAC-specific A2DP tests are not runnable on this remote as-is.

- rfkill ABSENT (binary not installed). Toggle the radio via `bluetoothctl power
  on/off` or `hciconfig hci0 up/down` instead of rfkill.

- LINGER=no for user pi. PipeWire runs as user services tied to a login session.
  Headless SSH automation must set XDG_RUNTIME_DIR=/run/user/<uid> (and reach the
  pipewire/pulse socket), or enable `loginctl enable-linger pi`. Operational, not
  a hard blocker.

Treat the above as operational caveats for the testbed.

## Version table

| Component        | Version / detail                                              |
|------------------|---------------------------------------------------------------|
| Distro           | Debian GNU/Linux 12 (bookworm), arm64                         |
| Kernel           | Linux 6.12.47+rpt-rpi-v8 (Raspberry Pi)                       |
| Hostname         | kpi                                                           |
| BlueZ            | bluetoothctl 5.66; bluetoothd active (pid 480)                |
| Audio server     | PipeWire 1.2.7 (ACTIVE)                                       |
| Session manager  | WirePlumber 0.4.13 (active)                                   |
| Pulse shim       | pipewire-pulse (active) -> provides PulseAudio API            |
| PulseAudio       | 16.1 installed but INACTIVE (not the running server)          |
| BlueALSA         | absent                                                        |

## Radio

- Controller hci0, BD_ADDR E4:5F:01:E6:31:85, Bus UART (on-board, not USB).
- Chip: Cypress Semiconductor (manufacturer 305) combo Wi-Fi/BT (the standard
  on-board Raspberry Pi radio, CYW434xx class). HCI/LMP 5.0.
- Class 0x7c0000; service classes Rendering, Capturing, Audio, Telephony.
- Roles: central + peripheral. Powered: yes. Pairable: yes. Discoverable: no
  (set discoverable when running the sink/fake-headphones test).
- A2DP SOURCE capability: yes (UUID 0x110a advertised; PipeWire bluez5 source).
- A2DP SINK capability: yes (UUID 0x110b advertised; PipeWire bluez5 sink).
- AVRCP target + controller present (media control alongside A2DP).
- Single controller only; no second adapter for simultaneous dual-role.

## Codec support (A2DP, via PipeWire libspa bluez5)

Available: SBC (mandatory), aptX / aptX-HD (libfreeaptx), LDAC (libldacBT_enc),
LC3 (LE Audio), Opus (+opus-g low-latency), FastStream.
Not available: AAC (no codec plugin, no libfdk-aac encoder).
Backing libs seen: libsbc.so.1, libfreeaptx.so.0, libldacBT_enc.so.2,
libldacBT_abr.so.2, libfaad.so.2 (AAC decode only).

## Audio tooling for the testbed scripts

Present: pw-cat, pw-play, pw-record (PipeWire); paplay, parecord (PulseAudio API
via pipewire-pulse); aplay, arecord (ALSA); sox; ffmpeg.
Absent: bluealsa, bluealsa-aplay, rfkill.
Pairing/scripting: bluetoothctl 5.66 (run non-interactively with piped commands).

## Recommended testbed approach: PIPEWIRE-based

Use the PipeWire path; do not rely on a standalone PulseAudio daemon or bluealsa.
pactl/paplay/parecord still work through pipewire-pulse if a Pulse-style interface
is preferred. For non-interactive SSH runs, export
`XDG_RUNTIME_DIR=/run/user/$(id -u)` so the CLI tools find the PipeWire socket
(or enable linger for pi).

- Fake-phone (A2DP SOURCE streaming a known sweep): from the Pi, pair/connect to
  the local DUT exposing an A2DP sink (`bluetoothctl` scripted: power on, pair,
  trust, connect). PipeWire then creates a Bluetooth output node for that sink.
  Generate a sweep with sox/ffmpeg and play it into the Bluetooth node via
  `pw-play`/`pw-cat --playback` (or `paplay -d <bluez_sink>`). Pick the codec by
  selecting the device profile/codec in WirePlumber; SBC is the safe default,
  aptX/LDAC for codec-specific runs.

- Fake-headphones (A2DP SINK recording arrivals to WAV): make the Pi discoverable
  and pairable (`bluetoothctl discoverable on`, pairable on, scripted agent to
  accept), let the local DUT connect as A2DP source. PipeWire exposes the incoming
  A2DP stream as a Bluetooth source node; capture it to WAV with
  `pw-record`/`parecord -d <bluez_source>`.

Constraints to respect in scripts: one role per test (single radio); SBC default,
no AAC; use bluetoothctl/hciconfig (not rfkill) to control the radio; ensure the
PipeWire user session/runtime dir is reachable over SSH.
