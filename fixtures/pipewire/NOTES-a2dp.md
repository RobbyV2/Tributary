# A2DP streaming source

Captured from live hardware (PipeWire 1.6.x, WirePlumber 0.5.x). Phone streaming A2DP into host.
Fixture: `pw-dump-a2dp-streaming.json` (full `pw-dump`, 97 objects).

A streaming A2DP source presents TWO nodes under one `bluez_card` device:

| node | media.class | ports | role |
|---|---|---|---|
| `bluez_input.<MAC>` | `Audio/Source` | `capture_MONO` | silent HFP endpoint |
| `bluez_input.<MAC_>.2` | `Stream/Output/Audio` | `output_FL`, `output_FR` | carries the real audio (SBC) |

Device: `bluez_card.<MAC_>` (`api.bluez5.address`, `media.class` Audio/Device).
The `.2` node alone carries `api.bluez5.profile = a2dp-source`, `api.bluez5.codec = sbc`, `factory.name = api.bluez5.a2dp.source`.
The `Audio/Source` node lacks any bluez5 profile prop; it is the mono HFP/voice path and stays silent while music plays.

Selection rule: prefer the audio-carrying node, the `Stream/Output/Audio` with `output_FL`/`output_FR`.
Do NOT pick the `Audio/Source` `capture_MONO` node; enumerating only `media.class == Audio/Source` selects the silent endpoint.

Ground truth links while streaming (`f-links.txt`): the `.2` `output_FL/FR` linked straight to `alsa_output...analog-stereo:playback_FL/FR`, while `capture_MONO` was the wrongly-linked silent node.
