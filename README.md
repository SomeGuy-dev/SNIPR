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
    snipr_compress.py
    snipr_start_loop.sh
    snipr_stop_loop.sh

bravo/
    snipr_republish.sh

common/
    snipr_protocol.py

systemd/
    snipr-republish.service
```

### Alpha

The Alpha side receives the source video and produces compressed image frames for transmission.

- `snipr_compress.py` implements the image-processing pipeline.
- `snipr_start_loop.sh` starts the capture/compression workflow.
- `snipr_stop_loop.sh` stops the Alpha processing loop.

### Common

`common/snipr_protocol.py` implements the SNIPR transport protocol, including packetization, frame identification, reassembly, and forward-error-correction support.

### Bravo

The Bravo side receives completed imagery and republishes it for consumption by the video distribution system.

`snipr_republish.sh` provides the current republishing workflow.

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

## License

See [LICENSE](LICENSE).
