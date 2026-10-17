# SNIPR

**SNIPR — Snapshot Network Imagery Protocol Relay**

SNIPR is an experimental low-bandwidth imagery transport system designed to move compressed image snapshots across a Meshtastic LoRa network and republish the received imagery as a video stream for ATAK.

The current reference implementation was developed around a **DJI Mini 4 Pro** using RTMP live streaming and **NATAK MESH Nucleus** nodes as the network platform.

## Overview

SNIPR converts a conventional video source into a sequence of highly compressed still images suitable for transport across a constrained LoRa link.

The basic data path is:

```text
DJI / RTMP video
       |
       v
  MediaMTX
       |
       v
SNIPR Alpha
  capture
  crop
  resize
  compress
       |
       v
SNIPR protocol
  fragmentation
  sequencing
  FEC
       |
       v
 Meshtastic LoRa
       |
       v
SNIPR Bravo
  reassembly
  FEC recovery
       |
       v
 MediaMTX / RTSP
       |
       v
      ATAK
```

SNIPR does not attempt to transport conventional full-motion video directly over LoRa. Instead, it trades frame rate and image quality for the ability to deliver useful imagery through a severely bandwidth-constrained radio network.

## Repository Layout

```text
alpha/
    snipr_compress.py          frame compressor v2.1 (JPEG, or HEVC/H.264 switched on by a config file)
    snipr_start_loop_v2.sh     capture loop v2: one open ffmpeg, paced to the radio
    snipr_start_loop.sh        capture loop v1 (reconnects for every picture)
    snipr_stop_loop.sh         stops either loop

bravo/
    snipr_decode.py            turns a reassembled HEVC/H.264/JPEG frame into a JPEG
    snipr_republish.sh         republisher v3.2: steady 5 pictures/s RTSP stream with an age overlay

common/
    snipr_protocol.py

systemd/
    snipr-republish.service

tools/
    bridge_hop_limit.py        per-deployment hop limit for the bridge's own packets (see docs/RELAYS.md)
    enable_snipr_cfg.py        adds or removes the snipr: block in /etc/nucleus/config.yaml
    move_snipr_runtime.py      moves SNIPR's working files from /tmp to /run/snipr
    loss_collect.sh            read-only log collection on a node, for loss analysis
    loss_analyze.py            pairs Alpha's sent packets with Bravo's receipts
    bravo_cpu.sh               read-only load and temperature check on Bravo

docs/
    MEASUREMENTS.md            what was measured and calculated, and how reliable each number is
    RELAYS.md                  hop limit, relays and the packet-loss lead
```

### Alpha

The Alpha side receives the source video and produces compressed image frames for transmission.

- `snipr_compress.py` implements the image-processing pipeline. It writes the same 160x120 JPEG as
  before until a one-line switch file (`/etc/nucleus/snipr_codec.conf`, for example
  `codec=hevc size=256x192 packets=7`) selects HEVC or H.264.
- `snipr_start_loop_v2.sh` is the current capture loop. It keeps one ffmpeg connected to the drone
  stream and times each cycle so a new picture reaches the bridge just before the previous one has
  finished sending. `snipr_start_loop.sh` is the original loop.
- `snipr_stop_loop.sh` stops the Alpha processing loop.
- Both loops keep their working files in `/run/snipr`, a RAM-backed folder (create it with
  `tools/move_snipr_runtime.py --tmpfiles`).

### Common

`common/snipr_protocol.py` implements the SNIPR transport protocol, including packetization, frame identification, reassembly, and forward-error-correction support.

### Bravo

The Bravo side receives completed imagery and republishes it for consumption by the video distribution system.

- `snipr_decode.py` decodes a reassembled HEVC/H.264 frame (or passes a JPEG through) onto a fixed
  640x480 canvas, so the stream ATAK sees never changes size.
- `snipr_republish.sh` (v3.2) serves the newest picture as RTSP at a steady 5 pictures per second,
  decodes in the background so the stream never stalls, and overlays the picture's age.

### systemd

`snipr-republish.service` is the reference systemd unit for the Bravo republisher.

## Design Goals

SNIPR is being developed to:

- operate over severely bandwidth-constrained Meshtastic LoRa links;
- preserve useful situational imagery rather than attempt conventional full-motion video;
- tolerate packet loss through frame reassembly and forward error correction;
- integrate with existing RTMP and RTSP video workflows;
- make received imagery consumable by ATAK;
- coexist with other services carried by the same Nucleus/Meshtastic platform.

## VLoRa / Voice Coexistence

The development platform also supports VLoRa Codec2 voice traffic.

Codec2 3200 voice testing has shown improved voice continuity when Codec2 data is aggregated into larger LoRa payloads. The current development configuration uses a 144-byte Codec2 payload containing 18 8-byte Codec2 frames.

Work is underway to allow SNIPR and VLoRa to share the same Meshtastic interface while giving active voice traffic priority.

The traffic-priority/coexistence implementation is currently experimental and should not yet be considered fully validated.

## Current Status

The repository contains the initial SNIPR reference implementation.

Development testing has demonstrated:

- RTMP source ingestion;
- image capture and compression;
- SNIPR packet fragmentation;
- frame reassembly;
- forward-error-correction support;
- transport over Meshtastic;
- received-image republishing;
- RTSP/ATAK integration;
- VLoRa Codec2 voice operation on the same development platform.

The repository now also holds the HEVC compressor and decoder, the v2 capture loop, the v3.2
republisher and the diagnostic tools. The numbers behind them, and how far each can be trusted, are in
[docs/MEASUREMENTS.md](docs/MEASUREMENTS.md). The main open item is packet loss (11-15% overall): a
likely cause is described in [docs/RELAYS.md](docs/RELAYS.md) but is not yet confirmed.

Additional installation documentation, sanitized configuration examples, protocol documentation, and coexistence testing are planned.

## Reference Platform

The current development environment includes:

- Linux-based NATAK MESH Nucleus nodes;
- Meshtastic LoRa;
- DJI Mini 4 Pro RTMP streaming;
- MediaMTX;
- FFmpeg;
- Python 3;
- ATAK;
- Codec2/VLoRa for experimental voice integration.

SNIPR is experimental software. Configuration values and service integration will vary between deployments.

## Security

Do not commit Meshtastic channel keys, private keys, credentials, API tokens, operational node identities, or other deployment-specific secrets to the repository.

Example configurations should use placeholder values where deployment-specific information is required.


