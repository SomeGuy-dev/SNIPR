#!/usr/bin/env python3
"""enable_snipr_cfg.py - turn the SNIPR image relay on (or off) in /etc/nucleus/config.yaml.

Run on EACH node (Alpha sends, Bravo receives; both need the block):

    sudo /opt/nucleus/venv/bin/python ~/enable_snipr_cfg.py --check     # dry run, changes nothing
    sudo /opt/nucleus/venv/bin/python ~/enable_snipr_cfg.py             # add the snipr: block
    sudo /opt/nucleus/venv/bin/python ~/enable_snipr_cfg.py --revert    # remove it again

Options: --hop-limit N (default 0)   --pacing SECS (default 0.45)

What it does (and does not do):
  * checks the INSTALLED nucleusd already has the SNIPR patch (cot_bridge block,
    snipr_protocol.py, SniprConfig in schema.py) - otherwise it stops;
  * appends one marked `snipr:` block to the END of the config (nothing else in
    the file is touched, comments and ordering stay as they are);
  * validates the result with the real nucleusd schema BEFORE writing anything;
  * keeps a timestamped backup next to the config;
  * does NOT restart anything and never transmits. The relay only starts when
    cot-bridge is restarted.
"""
import argparse
import datetime
import os
import re
import shutil
import socket
import sys

import yaml

BEGIN = "# --- SNIPR relay (enable_snipr_cfg.py) ---"
END = "# --- end SNIPR relay ---"
BRIDGE_MARK = "SNIPR image relay (snapshot imagery over LoRa"


def die(msg):
    print(f"\nSTOP: {msg}")
    sys.exit(1)


def block_text(hop, pacing):
    return (
        f"\n{BEGIN}\n"
        "snipr:\n"
        "  enabled: true\n"
        "  portnum: 259\n"
        f"  hop_limit: {hop}\n"
        f"  pacing_secs: {pacing}\n"
        f"{END}\n"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/nucleus/config.yaml")
    ap.add_argument("--hop-limit", type=int, default=0)
    ap.add_argument("--pacing", type=float, default=0.45)
    ap.add_argument("--check", action="store_true", help="dry run: show what would change, change nothing")
    ap.add_argument("--revert", action="store_true", help="remove the block this script added")
    a = ap.parse_args()

    if not (0 <= a.hop_limit <= 7):
        die("--hop-limit must be 0..7")
    if not (0.1 <= a.pacing <= 5.0):
        die("--pacing must be between 0.1 and 5.0 seconds")

    print(f"== SNIPR config on {socket.gethostname()} ==")
    if not a.check and os.geteuid() != 0:
        die("run it with sudo (it edits the config file).")

    # ---- 1. the installed code must already contain the SNIPR patch
    try:
        import nucleusd
    except Exception as e:  # noqa: BLE001
        die(f"cannot import nucleusd ({e}). Run it with /opt/nucleus/venv/bin/python.")
    pkg = os.path.dirname(nucleusd.__file__)
    bridge = os.path.join(pkg, "meshtastic", "cot_bridge.py")
    proto = os.path.join(pkg, "meshtastic", "snipr_protocol.py")
    schema = os.path.join(pkg, "schema.py")
    try:
        if BRIDGE_MARK not in open(bridge, encoding="utf-8").read():
            die(f"{bridge} has no SNIPR block. Apply apply_snipr_relay.py to the installed copy first.")
        if not os.path.isfile(proto):
            die(f"{proto} is missing. Apply apply_snipr_relay.py to the installed copy first.")
        if "class SniprConfig" not in open(schema, encoding="utf-8").read():
            die(f"{schema} has no SniprConfig. Apply apply_snipr_relay.py to the installed copy first.")
    except FileNotFoundError as e:
        die(f"installed file not found: {e}")
    print(f"   installed nucleusd: {pkg}")
    print("   SNIPR patch present: cot_bridge block, snipr_protocol.py, SniprConfig")

    # ---- 2. current config
    try:
        orig = open(a.config, encoding="utf-8").read()
        raw = yaml.safe_load(orig) or {}
    except Exception as e:  # noqa: BLE001
        die(f"cannot read/parse {a.config}: {e}")
    has_ours = BEGIN in orig and END in orig
    cur = raw.get("snipr")

    # ---- revert
    if a.revert:
        if not has_ours:
            if cur is not None:
                die("a snipr: section exists but was not added by this script; not touching it.")
            print("   nothing to revert (no snipr: section).")
            return
        new = re.sub(r"\n" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n", "", orig, flags=re.S)
        if "snipr" in (yaml.safe_load(new) or {}):
            die("revert would still leave a snipr: key; not writing.")
        if a.check:
            print("   --check: would remove the SNIPR block.")
            return
        bak = f"{a.config}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(a.config, bak)
        with open(a.config, "w", encoding="utf-8") as f:   # in place: keeps owner and mode
            f.write(new)
        print(f"   removed the SNIPR block (backup: {bak})")
        print("   Restart for it to take effect:  sudo systemctl restart cot-bridge")
        return

    # ---- add
    if cur is not None:
        print(f"   a snipr: section already exists: {cur}")
        print("   Not changing it. (Use --revert if this script added it.)")
        return
    blk = block_text(a.hop_limit, a.pacing)
    new = (orig if orig.endswith("\n") else orig + "\n") + blk
    try:
        cand = yaml.safe_load(new) or {}
    except Exception as e:  # noqa: BLE001
        die(f"the new text would not parse as YAML: {e}")
    want = {"enabled": True, "portnum": 259, "hop_limit": a.hop_limit, "pacing_secs": a.pacing}
    if cand.get("snipr") != want:
        die(f"parsed block is {cand.get('snipr')}, expected {want}.")
    if {k: v for k, v in cand.items() if k != "snipr"} != {k: v for k, v in raw.items()}:
        die("adding the block would change other keys; refusing.")

    # ---- 3. validate with the real schema (before writing)
    try:
        from nucleusd.schema import NucleusConfig
        validate = getattr(NucleusConfig, "model_validate", None) or NucleusConfig.parse_obj
    except Exception as e:  # noqa: BLE001
        validate = None
        print(f"   note: cannot import NucleusConfig ({e}); skipping schema validation")
    if validate is not None:
        try:
            validate(raw)
            base_ok = True
        except Exception as e:  # noqa: BLE001
            base_ok = False
            print(f"   note: the CURRENT config does not validate against the schema "
                  f"({str(e).splitlines()[0]}); skipping schema validation")
        if base_ok:
            try:
                validate(cand)
            except Exception as e:  # noqa: BLE001
                die(f"the schema rejects the new snipr: block:\n{e}")
            print("   schema validation: current config OK, with snipr: block OK")

    print("   block to add at the end of the config:")
    for line in blk.strip("\n").splitlines():
        print("      " + line)
    if a.check:
        print("   --check: nothing was changed.")
        return

    bak = f"{a.config}.bak-pre-snipr-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(a.config, bak)
    with open(a.config, "w", encoding="utf-8") as f:       # in place: keeps owner and mode
        f.write(new)
    print(f"   written. Backup: {bak}")
    print("   Not active yet. To start it:  sudo systemctl restart cot-bridge")
    print("   Then confirm:  journalctl -u cot-bridge -n 40 --no-pager | grep -i snipr")


if __name__ == "__main__":
    main()
