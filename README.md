![Tributary](assets/banner.png)

# Tributary

Software Bluetooth audio multiplexer for Linux, built on PipeWire and BlueZ.

Source devices connect to the host as an A2DP sink; PipeWire sums their audio and the host re-transmits the combined stream to a pair of A2DP headphones. The host's own output can join the mix as well.

## Requirements

- Linux
- PipeWire + WirePlumber
- BlueZ on the system bus
- Python 3.11+

## Setup

```sh
git clone <repo> Tributary && cd Tributary
python -m venv .venv
.venv/bin/pip install -e .
```

`scripts/setup-host.sh` is additive and reversible; it installs drop-in files only and never edits global configs in place. It adds you to the `bluetooth` group, installs a polkit rule for non-interactive `org.bluez` access, pins the adapter device class, enables both A2DP roles in WirePlumber, and installs the `tributary.service` user unit. Undo with `scripts/setup-host.sh --uninstall`.

```sh
./scripts/setup-host.sh
systemctl --user enable --now tributary
```

Without the installer, register the unit by hand at `~/.config/systemd/user/tributary.service`:

```ini
[Unit]
Description=Tributary Bluetooth audio multiplexer
After=pipewire.service wireplumber.service

[Service]
ExecStart=%h/Tributary/.venv/bin/trib run

[Install]
WantedBy=default.target
```

## Config

Copy `config/tributary.example.toml` to `~/.config/tributary.toml`:

```toml
headphone_mac = "B4:23:A2:01:6D:27"   # output headphones
allow_macs = ["E4:5F:01:E6:31:85"]    # source allowlist
sample_rate = 48000
reconcile_interval = 1.5

include_host_audio = true              # tap this machine's own output into the mix
host_source = "alsa_output.pci-0000_06_00.6.analog-stereo"  # omit for the default sink

[gains]
"E4:5F:01:E6:31:85" = 1.0
```

Set `include_host_audio` to fold the host's own playback into the mix; `host_source` names the sink whose monitor to capture, defaulting to the current sink when omitted.

## Usage

```sh
trib status                          # daemon state, codecs, adapter roles
trib list                            # connected sources, gain, mute
trib volume E4:5F:01:E6:31:85 0.8    # per-source gain
trib mute E4:5F:01:E6:31:85 on       # mute or unmute a source
trib headphone B4:23:A2:01:6D:27     # set the output headphone sink
trib graph                           # print the live link topology
```
