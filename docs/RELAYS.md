# Relays and the hop limit

SNIPR packets are sent with **hop limit 0**: they go straight from Alpha to Bravo and no other node
repeats them. The other traffic the Nucleus `cot_bridge` generates (ATAK forwarding, chat text and the
presence heartbeat) uses the radio's default hop limit of 3, so a neighbouring node repeats every one
of those packets.

## Why it matters on a two-node link

A node that repeats a packet waits an SNR-weighted random delay first (Meshtastic's flooding
algorithm: strong signals wait longest, which is by design on a busy mesh). While that packet sits at
the head of the transmit queue, everything queued behind it waits too, including SNIPR packets. When
the hold ends, the backlog goes out back to back. In one logged 16 s window on the development
platform that produced holds of about 2.0 s and 2.1 s, and the packets sent within 50 ms of the
previous one were never received intact (0 of 7), against 6 of 7 for packets sent 50-100 ms after the
previous one. That is a single short window of 19 packets: it is a lead, not a confirmed cause. See
[MEASUREMENTS.md](MEASUREMENTS.md#where-the-packet-loss-comes-from).

On a direct two-node link those repeats carry nothing useful, because the receiver already heard the
original. With three or more nodes, or a deliberate relay across difficult terrain, they are exactly
what you want.

## The setting

`tools/bridge_hop_limit.py` makes the hop limit of those bridge packets a per-deployment setting. It
does **nothing until you set a value**; unset, the bridge behaves exactly as before.

| Deployment | Setting |
|---|---|
| Two nodes talking directly | `set 0` |
| A relay or third node carries traffic | leave unset (radio default 3), or `set 2` / `set 3` |
| Larger mesh | leave unset |

```bash
# on each node, with the nucleus python
sudo /opt/nucleus/venv/bin/python bridge_hop_limit.py patch --check   # what would change
sudo /opt/nucleus/venv/bin/python bridge_hop_limit.py patch           # once: add the setting (default unchanged)
sudo /opt/nucleus/venv/bin/python bridge_hop_limit.py set 0
sudo systemctl restart cot-bridge                                       # between runs: this drops the relays briefly
/opt/nucleus/venv/bin/python bridge_hop_limit.py status
```

`unset` returns to the radio default and `unpatch` removes the code change again (byte for byte).
Use the same value on both nodes, because each node sets the hop limit of what it sends.

How it works: `patch` makes four exact-text edits to the installed `cot_bridge.py` (a helper block and
the three send calls) and refuses to write anything unless every edit matches exactly once. It keeps
the file's own line endings and makes a timestamped backup. `set` writes a systemd drop-in
(`cot-bridge.service.d/hop-limit.conf`) that sets `NUCLEUS_BRIDGE_HOP_LIMIT`, so no schema or
`config.yaml` change is involved. Values outside 0-7 are ignored with a warning in the log.

## Keeping relays and avoiding the holds

Hop limit 0 for bridge traffic is the simple answer for a two-node link. A setup that must both relay
and keep SNIPR flowing would need the relay hold itself to stop blocking SNIPR packets, which is a
property of the radio firmware's transmit queue rather than of anything in this repository. That has
not been investigated or tested here.
