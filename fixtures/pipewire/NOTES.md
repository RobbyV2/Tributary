# PipeWire/WirePlumber fixtures

Captured on this host (user `lobby`). All fixtures in this dir are real, unedited
tool output. Snapshots, not live streams.

## Graph notes

Four common assumptions, checked against reality on this host:

1. "each connected phone appears as a `bluez_input.*` source node" — PARTLY TRUE,
   with refinements the linker must handle:
   - A connected Bluetooth device (here: `Pixel Buds Pro 2`, MAC B4:23:A2:01:6D:27)
     does expose `bluez_input.B4:23:A2:01:6D:27` with `media.class=Audio/Source`.
     So the name prefix and class are right.
   - BUT the same physical device simultaneously produces THREE audio nodes:
     `bluez_input.<mac>` (Audio/Source), `bluez_output.<mac>.1` (Audio/Sink,
     factory api.bluez5.a2dp.sink), and `bluez_capture_internal.<mac>`
     (Stream/Input/Audio/Internal). Filtering must be exactly
     `media.class == "Audio/Source"` AND `node.name =~ ^bluez_input\.` or the
     reconciler will also grab the sink and the internal stream.
   - The MAC is formatted DIFFERENTLY per node: `bluez_input` keeps colons
     (`B4:23:A2:01:6D:27`); `bluez_output` uses underscores and a profile suffix
     (`B4_23_A2_01_6D_27.1`). Do not assume one normalization. To correlate the
     input/output halves of one device, use the shared `device.id` prop (here 81),
     not string surgery on the name.
   - PORT COUNT/CHANNELS are not stereo. This bluez_input source has a SINGLE
     output port `capture_MONO` (audio.channel=MONO), because the active profile
     is HFP-style mono mic capture. The high-quality stereo A2DP path is the
     OUTPUT (sink) direction, toward the buds. A phone/headset source can be mono.
     The linker must read the source's actual output ports rather than assuming
     FL/FR, and when feeding a stereo mix, fan the one MONO port into both
     `playback_FL` and `playback_FR` of the null sink.
   - Whether `bluez_input` exists at all, and its channel layout, depends on the
     active Bluetooth profile. In pure A2DP-sink-to-PC role the PC-side node and
     ports differ. Presence is profile-dependent, not guaranteed.

2. "a null sink `tributary_mix` can be created and summed into automatically when
   multiple links hit the same port" — SUPPORTED. No `tributary_mix` exists yet
   (must be created; see below). PipeWire's per-input-port mixer does sum every
   incoming link automatically, so linking several source output ports onto one
   `playback_FL` input port mixes them with no extra component. Confirmed behavior
   on this graph (pw-play's two outputs each land on distinct sink inputs; routing
   many outputs to one input is the standard summing case).

3. "monitor ports exist" — CONFIRMED. Every Audio/Sink here carries monitor ports:
   `alsa_output...` (id 50) has `monitor_FL/monitor_FR`; `bluez_output` (id 90) has
   `monitor_FL/monitor_FR`. They have `port.direction=out` and `port.monitor=true`.
   A null sink will expose the same. Read the merged mix from
   `tributary_mix:monitor_FL/FR`. Note an Audio/Source (like bluez_input) has NO
   monitor port; its capture port is already the output you tap.

4. "pw-dump / pw-link / pw-cli / wpctl are the control tools" — CONFIRMED present
   and working (versions below). pw-link is installed; `pw-link -l` works.

Additional caveats (not direct contradictions, but they will bite):

- HOTPLUG is real and central. The bluez nodes (ids 90/91/95) are ABSENT from
  pw-dump-idle.json and PRESENT in pw-dump-playing.json; the device connected /
  switched profile during the ~30s between captures. pw-dump is a one-shot
  snapshot. For live tracking the reconciler must watch registry add/remove events
  (e.g. `pw-cli` monitor / the libpipewire registry), not poll a single dump.
- `wpctl status` files `bluez_input` under "Filters", not "Sources" (it is a
  filter-backed virtual source). Do NOT parse wpctl section headers to find phone
  sources; trust pw-dump `media.class`. wpctl is fine for humans, weak for logic.
- `pw-play` is an alias of `pw-cat`; the client shows as `pw-cat` (id 87) while the
  node.name is `pw-play`. Identify nodes by node.name, not by client name.

## Commands and versions

pipewire 1.6.6, wireplumber 0.5.14, pw-cli 1.6.6, pw-dump 1.6.6 (see versions.txt).

Idle:    `pw-dump > pw-dump-idle.json`; `wpctl status > wpctl-status-idle.txt`;
         `pw-link -l > pw-link-idle.txt` (empty: no links exist with nothing playing).
Playing: tone `sox -n /tmp/trib_tone.wav synth 30 sine 440 gain -6`; played in
         background with `pw-play /tmp/trib_tone.wav &`; after the stream node
         registered captured `pw-dump > pw-dump-playing.json`,
         `wpctl status > wpctl-status-playing.txt`, `pw-link -l > pw-link-playing.txt`;
         then `kill <pid>` and `rm /tmp/trib_tone.wav`. System left as found.

## Playback stream node created (the test artifact)

id=83, node.name=`pw-play`, media.class=`Stream/Output/Audio`, media.name=
`/tmp/trib_tone.wav`, application.name=`pw-play`, client.id=87, stream.is-live=true.
Output ports: id 77 `output_FL` (ch FL), id 85 `output_FR` (ch FR), both
direction=out, format.dsp "32 bit float mono audio". WirePlumber auto-connected it
to the default sink `alsa_output...` (id 50) via links 80 and 78. This is the same
shape a `bluez_input` source presents to the graph: a node with output ports that
get linked onward.

## pw-dump object structure (what the reconciler keys on)

Every object: top-level `id` (global int; stable within a session, can be reused
after free) and `type`. Identity/config lives under `info.props` (a flat string
map). Prefer `object.serial` (monotonic, never reused) over `id` for dedup.

Node (`type=PipeWire:Interface:Node`), key info.props:
  node.name (stable string id, e.g. `bluez_input.B4:23:A2:01:6D:27`),
  node.description (human, e.g. `Pixel Buds Pro 2`),
  media.class (`Audio/Source` | `Audio/Sink` | `Stream/Output/Audio` | ...),
  device.id (links node to its Device; correlates the in/out halves of one BT
  device), factory.name (e.g. api.bluez5.a2dp.sink, support.null-audio-sink),
  object.path, object.serial, client.id; on sinks also api.bluez5.address /
  api.bluez5.profile / api.bluez5.codec.

Port (`type=PipeWire:Interface:Port`), key info.props:
  node.id (owning node global id), port.id (index within the node),
  port.name (`capture_MONO`, `playback_FL`, `monitor_FL`, `output_FL`),
  port.direction (`in` | `out`), audio.channel (`MONO` | `FL` | `FR`),
  format.dsp ("32 bit float mono audio"), port.monitor (true ONLY on monitor
  ports), object.path (`<node.name>:<role>_<idx>`, e.g.
  `bluez_input.B4:23:A2:01:6D:27:capture_0`), port.alias (`<desc>:<port.name>`).
  Source output port: direction out, name `capture_*`/`output_*`. Sink input port:
  direction in, name `playback_*`. Monitor: direction out, port.monitor=true,
  name `monitor_*`.

Link (`type=PipeWire:Interface:Link`), key info.props:
  link.output.node, link.output.port (source side, BY GLOBAL ID),
  link.input.node, link.input.port (dest side, BY GLOBAL ID),
  link.async, factory.id (21 = link-factory), client.id.
  Links reference node and PORT global ids, never names; to display names you join
  each port id back to its Port object. Multiple links into one input port are
  summed by PipeWire automatically.

## pw-link text format (human view)

`pw-link -l` lists existing links as `<node.name>:<port.name>` with a child line
per peer: `|-> peer` for an outgoing link from an output port, `|<- peer` for an
incoming link into an input port. Empty when no links exist. Create links by name:
`pw-link "<out node.name>:<out port>" "<in node.name>:<in port>"`. Example observed:
`pw-play:output_FL  |-> alsa_output.pci-0000_06_00.6.analog-stereo:playback_FL`.

## Null sink (tributary_mix) — how it will appear

Create with either
`pw-cli create-node adapter '{ factory.name=support.null-audio-sink node.name=tributary_mix media.class=Audio/Sink object.linger=true audio.position=[FL FR] }'`
or `pactl load-module module-null-sink sink_name=tributary_mix channel_map=front-left,front-right`.
It then shows as an Audio/Sink node with input ports `playback_FL`/`playback_FR`
(direction in) and monitor ports `monitor_FL`/`monitor_FR` (direction out,
port.monitor=true) — structurally identical to `alsa_output` (id 50) and
`bluez_output` (id 90) captured here. Pipeline: link each phone source's output
port(s) to `tributary_mix:playback_FL/FR` (fan a MONO source into both), let
PipeWire sum at the input ports, and tap the merged result from
`tributary_mix:monitor_FL/FR`.
