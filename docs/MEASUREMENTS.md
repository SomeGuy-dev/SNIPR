# SNIPR measurements

Reference platform: two Nucleus nodes (Raspberry Pi 4B + RAK6421), meshtasticd 2.7.x, `SHORT_FAST`
(SF7, 250 kHz, coding rate 4/5), 30 dBm, US 902-928 MHz. Source: DJI Mini 4 Pro over RTMP.

Each number is marked **measured** (from logs and recordings on this platform) or **calculated**
(from the LoRa airtime formula or a model fitted to the measurements). These are development results
from a short-range two-node setup. They say how the system behaves here, not how it will behave at
range or in the field.

## The picture on the air

| | |
|---|---|
| Format | HEVC 256x192, switched on with `codec=hevc` in `/etc/nucleus/snipr_codec.conf` (JPEG 160x120 is still the default and the fallback) |
| Packets | 7 data + 1 XOR parity |
| Packet | 5-byte header + 223 bytes payload = 228 bytes, 252 bytes on air |
| Air time per packet | 201 ms (measured; matches the Semtech formula) |
| Pacing in `cot_bridge` | 0.45 s per packet, so about 0.249 s idle between packets |
| Hop limit | 0 |
| Picture time | about 3.6 s for the 8 packets |

## How old is the picture in ATAK (measured)

| | |
|---|---|
| Delivery delay, capture to ATAK | 12.6 s |
| Average age of the scene on screen | about 16 s |
| Oldest scene on screen | 28 s |
| Longest stretch with no new picture | 15.9 s |
| Packet loss | 11-15% overall; about 4% when packets are spaced normally |
| Cycles that never completed | 31% |

Age model (calculated, reproduces the measured 16 s): mean age = D + P(1+q) / (2(1-q)), with
D = 12.6 s delivery delay, P about 3.7 s picture period and q the share of cycles that never complete.

## If the loss were fixed (calculated)

| | Today | Realistic (4% packet loss) | No loss |
|---|---|---|---|
| Pictures reaching ATAK per minute | 11.2 | 15.6 | 16.2 |
| Average age of the scene on screen | 16.1 s | 14.6 s | 14.4 s |
| Oldest scene on screen | 28 s | about 17 s | about 16 s |
| Longest stretch with no new picture | 15.9 s | about 4 s | 3.7 s |

Sharpness, air time and the 12.6 s delay do not change, because lost packets are not re-sent.
Fixing loss recovers about 1.5 s of the 16 s average.

## Where the packet loss comes from

Not a weak signal: SNR at the receiver averaged 12 dB, far above the SF7 limit of about -7.5 dB.
Overall loss is 11-15% and it clumps.

Lead mechanism (a single 16 s paired log of 19 packets; **not yet confirmed on a longer run**): the
sender repeats other nodes' packets with an SNR-weighted delay (about 2.0 s and 2.1 s in the window). That
hold stalls the whole transmit queue, and the SNIPR packets queued behind it then go out back to back.

| Idle gap before the packet | Packets | Received intact | Damaged | Never seen |
|---|---|---|---|---|
| under 50 ms | 7 | 0 | 2 | 5 |
| 50-100 ms | 7 | 6 | 0 | 1 |
| 100 ms or more, or first | 5 | 3 | 1 | 1 |

Three damaged packets arrived with their first four bytes missing. Loss in that window was 53%.

A second, separate effect from an earlier voice test (72-byte packets): a packet was lost 9 of 9
times when it began before the receiver had delivered the previous one, against 2 of 62 otherwise.
At today's spacing this alone should be harmless.

Still untested: power dips on the sender during transmit bursts (the `loss_collect.sh` script records
under-voltage events), and interference. See [RELAYS.md](RELAYS.md) for the setting that removes the
relays, and `tools/loss_collect.sh` / `tools/loss_analyze.py` to repeat the measurement.

## Spreading factor (calculated)

Packet time is 252 bytes at 250 kHz. The picture time keeps today's 0.249 s idle gap, because
short gaps already raise loss.

| Preset / SF | Packet air time | 8-packet picture | Link budget gain vs SF7 | Range multiple (path-loss exponent 2 / 3 / 5) |
|---|---|---|---|---|
| SF7 (`SHORT_FAST`) | 201 ms | 3.6 s | - | 1 |
| SF8 | 357 ms | 4.8 s | +2.5 dB | 1.33 / 1.21 / 1.12 |
| SF9 | 642 ms | 7.1 s | +5 dB | 1.78 / 1.47 / 1.26 |
| SF10 | 1.16 s | 11.3 s | +7.5 dB | - |
| SF11 | 2.12 s | 18.9 s | +10 dB | - |

An orchard study at 904 MHz fitted a path-loss exponent of 5.07 through foliage, which is why the
forest range gain of a higher SF is small. Doubling the bandwidth costs about 3 dB.

What depends on the spreading factor in this code: `SNIPR_PACING_SECS` (0.45 s), the loop's
`SNIPR_TX_MS_PER_PACKET` (450) and `packets=` in `/etc/nucleus/snipr_codec.conf`, and the radio preset,
which must be identical on every node. The packet header, reassembler and parity are independent of it.
LONG_MODERATE would take about 64 s per picture, longer than the 60 s reassembly timeout.

Codec2 voice (`stream_enabled`) sends 99-byte packets per 180 ms of audio. Air time per packet is 89 ms
(49% of real time) at SF7, 162 ms (90%) at SF8 and 293 ms (163%) at SF9, so voice stops working at SF9.

## Picture quality against packet budget (measured, six indoor drone frames)

| Data packets | Size | Bytes | SSIM against source | AS OF stamp |
|---|---|---|---|---|
| 7 (today) | 256x192 | 1,490 | 0.83 | readable |
| 5 | 256x192 | 1,060 | 0.79 | readable |
| 4 | 256x192 | 850 | 0.77 | readable |
| 3 | 256x192 | 600 | 0.74 | garbled |
| 2 | 160x120 | 410 | 0.72 | unreadable |

192x144 is no better than 256x192 at the same byte count. No colour or brightness processing is applied.

## Parity options (calculated)

Completion odds per picture at 10% / 14% / 18% packet loss:

| Design | Completion | Picture time | Score |
|---|---|---|---|
| Today: 7 data + 1 XOR | 81 / 69 / 56% | 3.6 s | 0.83 |
| 7 data + 2 parity | 95 / 88 / 79% | 4.05 s | 0.83 |
| 6 + 2 | 96 / 91 / 84% | 3.6 s | 0.81 |
| 5 + 2 | 97 / 94 / 88% | 3.15 s | 0.79 |
| 5 + 1 XOR (config change only) | 89 / 80 / 70% | 2.7 s | 0.79 |

Two parity packets need a GF(256)-style code on both ends (about 40 lines). Not implemented.
