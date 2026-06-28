# BlueZ fixtures

Captured on host `lobby` against the running `org.bluez` daemon on the SYSTEM bus.
Everything here is live-introspected unless explicitly marked as documented. Tools:
busctl 257-style, gdbus 2.x, bluetoothctl 5.86.

## Hardware notes

1. Adapter count. This host has ONE radio: `/org/bluez/hci0` (rfkill also lists only
   `hci0`). A dedicated-pair layout (one A2DP sink + one A2DP source) needs a second USB
   dongle here. See point 2: one adapter already carries both roles, so the split is
   optional rather than required.

2. A single adapter can hold A2DP sink AND source simultaneously -> CONFIRMED.
   `org.bluez.Media1.SupportedUUIDs` on `hci0` = `[0000110a (A2DP Source),
   0000110b (A2DP Sink)]`, and the adapter's own `UUIDs` advertise both Audio Source
   (110a) and Audio Sink (110b), plus AVRCP target/controller and HFP/HFP-AG. So the
   single-adapter dual-role assumption holds on this hardware.

3. Adapter audio device class -> set at config time, not over D-Bus.
   `Adapter1.Class` is READ-ONLY over D-Bus (`access="read"`, no `writable` flag). You
   cannot set the major device class at runtime through a property write. The current
   `Class = 0x7C010C` decodes to major = Computer (0x01) / minor = Laptop (0x03) with the
   service-class bits Rendering+Capturing+Object-Transfer+Audio+Telephony set. The Audio
   SERVICE bit (bit 21) is already on, but the MAJOR device class is Computer, so a
   scanning phone sees a computer, not headphones. To present as an Audio/Video device
   (major 0x04, e.g. headset minor) you must set `[General] Class=` in
   `/etc/bluetooth/main.conf` and restart `bluetoothd`; the audio service bits are then
   OR'd in automatically from the registered A2DP/HFP service records. Net: no runtime
   D-Bus path to change device class; it is config-time.

4. Agent1 NoInputNoOutput auto-accepts, broadly true with a caveat. With
   capability `NoInputNoOutput`, Secure Simple Pairing uses Just Works, so the agent's
   `RequestConfirmation`/`RequestPasskey`/`RequestPinCode` are normally NOT invoked. The
   daemon must still implement `AuthorizeService(o, s)` (called for incoming service
   connections from non-Trusted devices) and `Release`/`Cancel`. Marking a device
   `Trusted=true` suppresses `AuthorizeService`. No agent is currently registered, so this
   could not be live-introspected.

5. `org.bluez` is on the SYSTEM bus -> CONFIRMED. All `busctl --system` / `gdbus --system`
   calls succeeded; no session-bus name. Daemon owns `org.bluez` on the system bus.

6. Device1 has no `Profiles` property. BlueZ exposes
   per-service `UUIDs` (`as`, read-only) plus `ConnectProfile(s uuid)` /
   `DisconnectProfile(s uuid)`. There is no `Profiles` member. Use `UUIDs` to enumerate
   supported services.

(Other than the above: none found.)

## Versions

- bluetoothctl: 5.86
- bluetoothd: 5.86 (resolved via one of the common daemon paths)
- Tooling present: busctl, gdbus, bluetoothctl, rfkill, dbus-send. `hciconfig` is ABSENT
  (deprecated; modern BlueZ ships only `bluetoothctl` + D-Bus, not the legacy hci-tools).

## Topology

- Single adapter: `/org/bluez/hci0`, address `14:AC:60:B5:45:8E` (public), name/alias
  "lobby". Roles: central + peripheral (dual-role LE). Powered=true, Pairable=true,
  Discoverable=false. `Discovering` is transient (observed flipping true/false between
  captures because a scan was active).
- Three child devices (all `Connected=false` at capture):
  - `dev_B4_23_A2_01_6D_27` Pixel Buds Pro 2 (Icon audio-headset, Class 0x244404 =
    Audio/Video major / Wearable-Headset minor). Paired+Bonded+Trusted. Interfaces:
    Device1, Bearer.BREDR1, Input1, MediaControl1. The representative audio peer.
  - `dev_00_07_04_67_96_C0` and `dev_E0_AE_5E_31_3B_A7` PLAYSTATION(R)3 Controllers
    (Class 1288, input-gaming). Not paired. Interfaces: Device1, Bearer.BREDR1, Input1.
- Adapter-level manager interfaces co-located on `hci0`: `org.bluez.Media1`,
  `org.bluez.GattManager1`, `org.bluez.LEAdvertisingManager1`,
  `org.bluez.NetworkServer1`, `org.bluez.BatteryProviderManager1`.

## org.bluez.Adapter1 (live, on /org/bluez/hci0)

Methods: `StartDiscovery()`, `StopDiscovery()`, `SetDiscoveryFilter(a{sv} properties)`,
`GetDiscoveryFilters() -> as`, `RemoveDevice(o device)`. (No adapter-level Connect;
connection is per-Device1.)

Writable props: `Alias s`, `Powered b`, `Connectable b`, `Discoverable b`,
`DiscoverableTimeout u`, `Pairable b`, `PairableTimeout u`.
Read-only props: `Address s`, `AddressType s`, `Name s`, `Class u`, `PowerState s`,
`Discovering b`, `UUIDs as`, `Modalias s`, `Roles as`, `ExperimentalFeatures as`,
`Manufacturer q`, `Version y`.

Current values of note: `Class=8126732` (0x7C010C, Computer/Laptop + Audio service bit;
see contradiction 3), `Powered=true`, `Discoverable=false`, `Pairable=true`.

## org.bluez.Device1 (live, from Pixel Buds dev_B4_23_A2_01_6D_27)

Methods: `Connect()`, `Disconnect()`, `ConnectProfile(s UUID)`,
`DisconnectProfile(s UUID)`, `Pair()`, `CancelPairing()`.
Key props: `Connected b` (READ-ONLY), `Paired b`, `Bonded b`, `Trusted b` (rw),
`Blocked b` (rw), `UUIDs as` (read-only; service list), `Adapter o`, `Address s`,
`Name s`, `Alias s` (rw), `Class u`, `Icon s`, `ServicesResolved b`,
`WakeAllowed b` (rw), plus LE scan props (RSSI, TxPower, ManufacturerData, ServiceData,
AdvertisingData/Flags, Appearance, Sets).
New in 5.86: an `org.bluez.Bearer.BREDR1` interface accompanies Device1 and carries a
`Disconnected(s name, s message)` signal (busctl lists it under both Bearer.BREDR1 and
Device1).

## org.bluez.AgentManager1 (live, on /org/bluez)

`RegisterAgent(o agent, s capability)`, `RequestDefaultAgent(o agent)`,
`UnregisterAgent(o agent)`.

### org.bluez.Agent1 (documented; no agent registered, not live-introspectable)

The daemon must implement and export these for the agent object it registers:
`Release()`, `RequestPinCode(o device) -> s`, `DisplayPinCode(o device, s pincode)`,
`RequestPasskey(o device) -> u`, `DisplayPasskey(o device, u passkey, q entered)`,
`RequestConfirmation(o device, u passkey)`, `RequestAuthorization(o device)`,
`AuthorizeService(o device, s uuid)`, `Cancel()`. Register with capability string
"NoInputNoOutput" for headless Just-Works auto-accept (see contradiction 4).

## org.bluez.ProfileManager1 (live, on /org/bluez)

`RegisterProfile(o profile, s uuid, a{sv} options)`, `UnregisterProfile(o profile)`.

## org.bluez.Media1 (live, on /org/bluez/hci0)

Methods: `RegisterEndpoint(o endpoint, a{sv} properties)`, `UnregisterEndpoint(o)`,
`RegisterPlayer(o player, a{sv} properties)`, `UnregisterPlayer(o)`,
`RegisterApplication(o application, a{sv} options)`, `UnregisterApplication(o)`.
Props: `SupportedUUIDs as` = [A2DP Source 0000110a, A2DP Sink 0000110b],
`SupportedFeatures as` = [tx-timestamping].

## Dynamic media objects (MediaTransport1 / MediaEndpoint1 / MediaPlayer1)

None live: every device is `Connected=false`, so no transport/endpoint/player objects
exist (busctl-tree shows only `dev_*` nodes; no `/sepN`, `/fdN`, `/playerN`). These are
created on demand once a device connects and audio is set up. Documented path patterns:
endpoint/sep -> `/org/bluez/hci0/dev_XX/sepN`; transport ->
`/org/bluez/hci0/dev_XX/sepN/fdM`; player -> `/org/bluez/hci0/dev_XX/playerN`. The paired
Pixel Buds expose the legacy `org.bluez.MediaControl1` (all methods marked deprecated;
props `Connected b`, `Player o`) which superseded-by MediaPlayer1/MediaTransport1.

## File index

- versions.txt, busctl-tree.txt, root-introspect.txt, getmanagedobjects.txt
- adapter1-introspect-hci0.txt (busctl table + gdbus XML, all hci0 interfaces)
- device1-introspect.txt (Pixel Buds full + gdbus XML; PS3 controller for the
  Input-only variant)
- media-introspect.txt (Media1 + MediaControl1; dynamic-object notes)
- bluetoothctl-show.txt, bluetoothctl-list.txt, hciconfig.txt (absent), rfkill.txt
