#!/usr/bin/env python3
"""bridge_hop_limit.py - make the hop limit of the cot-bridge's own packets a per-deployment setting.

Why: cot_bridge sends its ATAK-forward packets, chat text and presence heartbeat with the radio's
default hop limit (3), so every neighbour relays them. SNIPR and voice already send with hop limit 0.
On a two-node direct link those relays are pure overhead: each relayed packet makes the other node
hold its transmit queue for an SNR-weighted delay (about 2 s for a strong signal), and SNIPR packets
queued behind it then leave back to back and are lost far more often.

This tool keeps relaying possible. Nothing changes until you set a value, and unsetting it restores the
radio default:

    two nodes talking directly            -> set 0
    a relay / third node carries traffic  -> leave it unset (radio default), or set 2 or 3

Run it with the nucleus python so it can find the installed code (or pass --file):

    sudo /opt/nucleus/venv/bin/python bridge_hop_limit.py patch           # once: add the setting (default unchanged)
    sudo /opt/nucleus/venv/bin/python bridge_hop_limit.py set 0           # use hop limit 0 for these packets
    sudo /opt/nucleus/venv/bin/python bridge_hop_limit.py unset           # back to the radio default
    sudo /opt/nucleus/venv/bin/python bridge_hop_limit.py unpatch         # remove the code change again
    /opt/nucleus/venv/bin/python bridge_hop_limit.py status

Add --check to patch / unpatch / set / unset to see what would happen without changing anything.

What it does and does not do:
  * patch edits installed cot_bridge.py in four places (three send calls plus one helper block). Each
    edit is an exact-text match; if any of them is not found exactly once nothing is written.
  * set / unset write or remove a systemd drop-in (cot-bridge.service.d/hop-limit.conf) that sets
    NUCLEUS_BRIDGE_HOP_LIMIT. No schema or config.yaml change, so nucleusd validation is unaffected.
  * it never restarts anything and never transmits. The new value is read when cot-bridge starts:
    sudo systemctl restart cot-bridge   (this briefly drops the SNIPR / voice relays, so do it between runs).
  * the same value must be set on both nodes, because each node sets the hop limit of what it sends.
"""
import argparse
import datetime
import os
import subprocess
import sys

MARK = "# --- bridge hop limit (snipr tools/bridge_hop_limit.py)"
ENV = "NUCLEUS_BRIDGE_HOP_LIMIT"
DROPIN_DIR = "/etc/systemd/system/cot-bridge.service.d"
DROPIN = os.path.join(DROPIN_DIR, "hop-limit.conf")

HELPER = '''
''' + MARK + ''' ----
# Hop limit for the ATAK forward, chat text and presence heartbeat packets. Unset = the radio's own
# default, exactly as before. Set NUCLEUS_BRIDGE_HOP_LIMIT=0 on a two-node direct link so these packets
# are not relayed; leave it unset (or use 2-3) when relays carry traffic across a larger mesh.
def _bridge_hop_limit():
    raw = os.environ.get("NUCLEUS_BRIDGE_HOP_LIMIT", "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        n = -1
    if 0 <= n <= 7:
        return n
    logger.warning("NUCLEUS_BRIDGE_HOP_LIMIT=%r is not 0..7; using the radio default" % raw)
    return None


BRIDGE_HOP_LIMIT = _bridge_hop_limit()


def _bridge_hop_kwargs():
    return {} if BRIDGE_HOP_LIMIT is None else {"hopLimit": BRIDGE_HOP_LIMIT}


def _send_text(iface, text):
    if BRIDGE_HOP_LIMIT is None:
        return iface.sendText(text, wantAck=False)
    import inspect
    if "hopLimit" in inspect.signature(iface.sendText).parameters:
        return iface.sendText(text, wantAck=False, hopLimit=BRIDGE_HOP_LIMIT)
    # older meshtastic library: sendText has no hopLimit, so send the same packet through sendData
    return iface.sendData(text.encode("utf-8"), portNum=TEXT_MESSAGE_PORTNUM, wantAck=False,
                          hopLimit=BRIDGE_HOP_LIMIT)
# --- end bridge hop limit ----
'''

# Each edit is (name, [(old, new), ...]). Exactly one variant must match, exactly once, or nothing is
# written. Two variants exist where the bridge differs between Nucleus releases (the heartbeat send).
EDITS = [
    ("helper block after TEXT_MESSAGE_PORTNUM", [
        ("TEXT_MESSAGE_PORTNUM = 1\n",
         "TEXT_MESSAGE_PORTNUM = 1\n" + HELPER)]),
    ("chat text send", [
        ("iface.sendText(text, wantAck=False)\n            stats[\"text_tx\"]",
         "_send_text(iface, text)\n            stats[\"text_tx\"]")]),
    ("ATAK forward send", [
        ("iface.sendData(wire_bytes, portNum=ATAK_FORWARDER_PORTNUM, wantAck=False)",
         "iface.sendData(wire_bytes, portNum=ATAK_FORWARDER_PORTNUM, wantAck=False, **_bridge_hop_kwargs())")]),
    ("presence heartbeat send", [
        (r'iface.sendData(b"\x01", portNum=HEARTBEAT_PORTNUM, wantAck=False)',
         r'iface.sendData(b"\x01", portNum=HEARTBEAT_PORTNUM, wantAck=False, **_bridge_hop_kwargs())'),
        ("iface.sendData(_build_heartbeat_payload(),\n                       portNum=HEARTBEAT_PORTNUM, wantAck=False)",
         "iface.sendData(_build_heartbeat_payload(),\n                       portNum=HEARTBEAT_PORTNUM, wantAck=False, **_bridge_hop_kwargs())")]),
]


def die(msg):
    print("\nSTOP: " + msg)
    sys.exit(1)


def find_bridge(path):
    if path:
        return path
    try:
        import nucleusd
    except Exception as e:  # noqa: BLE001
        die("cannot import nucleusd (%s). Run it with /opt/nucleus/venv/bin/python, or pass --file." % e)
    return os.path.join(os.path.dirname(nucleusd.__file__), "meshtastic", "cot_bridge.py")


def read(path):
    """Return the file text exactly as stored (line endings untouched)."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except OSError as e:
        die("cannot read %s: %s" % (path, e))


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:     # in place: keeps owner and mode
        f.write(text)


def apply_edits(src, forward):
    """Return the edited text, or stop. Every edit must match exactly once (one variant each).
    Works on LF text and puts the file's own line endings back (the installed copy may be CRLF)."""
    crlf = "\r\n" in src
    if crlf and src.count("\n") != src.count("\r\n"):
        die("the file mixes CRLF and LF line endings; not touching it.")
    text = src.replace("\r\n", "\n") if crlf else src
    if "\r" in text:
        die("the file contains stray carriage returns; not touching it.")
    for name, variants in EDITS:
        hits = [(o, n) for o, n in variants if text.count(n if not forward else o) == 1]
        if len(hits) != 1:
            die("%s: expected exactly one match, found %d. Nothing was changed.\n"
                "       (The installed cot_bridge differs from the version this tool was written for.)"
                % (name, len(hits)))
        old, new = hits[0]
        a, b = (old, new) if forward else (new, old)
        text = text.replace(a, b)
    return text.replace("\n", "\r\n") if crlf else text


def need_root(check):
    if not check and os.geteuid() != 0:
        die("run it with sudo (it edits files under /opt and /etc).")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def cmd_patch(a, forward):
    bridge = find_bridge(a.file)
    src = read(bridge)
    has = MARK in src
    if forward and has:
        print("   already patched: " + bridge)
        return
    if not forward and not has:
        print("   not patched, nothing to remove: " + bridge)
        return
    new = apply_edits(src, forward)
    try:
        compile(new.replace("\r\n", "\n"), bridge, "exec")
    except SyntaxError as e:
        die("the edited file would not compile (%s). Nothing was changed." % e)
    if forward and MARK not in new or not forward and MARK in new:
        die("internal check failed. Nothing was changed.")
    print("   %s: %d edits %s" % (bridge, len(EDITS), "would be made" if a.check else "ok"))
    if a.check:
        return
    need_root(False)
    bak = "%s.bak-hoplimit-%s" % (bridge, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    write(bak, src)
    write(bridge, new)
    print("   backup: " + bak)
    if forward:
        print("   Default behaviour is unchanged. Next:  set 0  (two-node link), then restart cot-bridge.")
    else:
        print("   Restart for it to take effect:  sudo systemctl restart cot-bridge")


def cmd_set(a):
    n = a.value
    if not 0 <= n <= 7:
        die("hop limit must be 0..7")
    bridge = find_bridge(a.file)
    if MARK not in read(bridge):
        die("%s is not patched yet. Run 'patch' first." % bridge)
    text = "[Service]\nEnvironment=%s=%d\n" % (ENV, n)
    print("   would write %s:\n%s" % (DROPIN, "".join("      " + l + "\n" for l in text.splitlines())) if a.check
          else "   writing " + DROPIN)
    if a.check:
        return
    need_root(False)
    os.makedirs(DROPIN_DIR, exist_ok=True)
    with open(DROPIN, "w", encoding="utf-8") as f:
        f.write(text)
    r = run(["systemctl", "daemon-reload"])
    print("   daemon-reload: " + ("ok" if r.returncode == 0 else "failed: " + r.stderr.strip()))
    print("   Takes effect when cot-bridge restarts:  sudo systemctl restart cot-bridge")
    print("   Set the same value on the other node too.")


def cmd_unset(a):
    if not os.path.exists(DROPIN):
        print("   no drop-in at %s; already on the radio default." % DROPIN)
        return
    if a.check:
        print("   would remove " + DROPIN)
        return
    need_root(False)
    os.remove(DROPIN)
    try:
        os.rmdir(DROPIN_DIR)
    except OSError:
        pass
    r = run(["systemctl", "daemon-reload"])
    print("   removed %s; daemon-reload: %s" % (DROPIN, "ok" if r.returncode == 0 else "failed: " + r.stderr.strip()))
    print("   Takes effect when cot-bridge restarts:  sudo systemctl restart cot-bridge")


def cmd_status(a):
    bridge = find_bridge(a.file)
    print("   cot_bridge: %s" % bridge)
    print("   code patched: %s" % ("yes" if MARK in read(bridge) else "no"))
    if os.path.exists(DROPIN):
        print("   drop-in: " + read(DROPIN).strip().replace("\n", " "))
    else:
        print("   drop-in: none (radio default hop limit)")
    r = run(["systemctl", "show", "cot-bridge", "-p", "Environment", "--value"])
    if r.returncode == 0:
        print("   running service environment: " + (r.stdout.strip() or "(empty)"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", help="path to cot_bridge.py (default: the installed nucleusd copy)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("patch", "unpatch", "unset", "status"):
        p = sub.add_parser(name)
        if name != "status":
            p.add_argument("--check", action="store_true", help="show what would happen, change nothing")
    p = sub.add_parser("set")
    p.add_argument("value", type=int)
    p.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if not hasattr(a, "check"):
        a.check = False
    print("== bridge hop limit on %s ==" % os.uname().nodename)
    if a.cmd in ("patch", "unpatch"):
        need_root(a.check)
        cmd_patch(a, a.cmd == "patch")
    elif a.cmd == "set":
        cmd_set(a)
    elif a.cmd == "unset":
        cmd_unset(a)
    else:
        cmd_status(a)


if __name__ == "__main__":
    main()
