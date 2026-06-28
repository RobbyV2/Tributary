# Fixtures

Real, unedited tool output captured against the live systems on both machines.
Everything downstream is tested against these fixtures, not against remembered API
shapes.

Three directories, each with raw captures plus a `NOTES.md` that interprets them.

## bluez/

`org.bluez` introspected over the system D-Bus on the host (BlueZ 5.86).

- `versions.txt` tool versions and presence.
- `busctl-tree.txt` object tree under org.bluez.
- `root-introspect.txt`, `getmanagedobjects.txt` manager interfaces and the full
  managed-object map.
- `adapter1-introspect-hci0.txt` Adapter1 on hci0 (methods, properties, current
  values; busctl table plus gdbus XML).
- `device1-introspect.txt` Device1 (paired Pixel Buds Pro 2, full; a PS3 controller
  for the input-only variant).
- `media-introspect.txt` Media1 and MediaControl1, plus dynamic-object path notes.
- `bluetoothctl-show.txt`, `bluetoothctl-list.txt` adapter and device summaries.
- `hciconfig.txt` records that hciconfig is absent on modern BlueZ.
- `rfkill.txt` radio block state (single radio: hci0).

## pipewire/

PipeWire and WirePlumber graph snapshots on the host (PipeWire 1.6.6, WirePlumber
0.5.14), captured idle and again while a synthetic tone played.

- `versions.txt` tool versions.
- `pw-dump-idle.json`, `pw-dump-playing.json` full graph snapshots; the bluez nodes
  are absent at idle and present while playing, which is the hotplug case the
  reconciler tracks via registry events.
- `wpctl-status-idle.txt`, `wpctl-status-playing.txt` human-readable status.
- `pw-link-idle.txt`, `pw-link-playing.txt` existing links (idle is empty).

## remote/

Inventory of the SSH test host pi@kpi.local, captured read-only (Debian 12
bookworm, arm64; BlueZ 5.66, PipeWire 1.2.7, WirePlumber 0.4.13).

- `remote-versions.txt` BlueZ, PipeWire, WirePlumber, PulseAudio versions.
- `remote-os.txt` distro, kernel, architecture, hostname.
- `remote-bluetooth.txt` controller, address, roles, advertised A2DP UUIDs.
- `remote-codecs.txt` A2DP codec plugins present (no AAC).
- `remote-audio-tools.txt` audio and pairing tooling available for the testbed.

## Versions at a glance

| Component   | Host                           | Remote (pi@kpi.local)            |
|-------------|--------------------------------|----------------------------------|
| Distro      | CachyOS Linux (Arch-based)     | Debian 12 (bookworm)             |
| Kernel      | 7.0.9-12-cachyos-vfio (x86_64) | 6.12.47+rpt-rpi-v8 (arm64)       |
| BlueZ       | 5.86                           | 5.66                             |
| PipeWire    | 1.6.6                          | 1.2.7                            |
| WirePlumber | 0.5.14                         | 0.4.13                           |
