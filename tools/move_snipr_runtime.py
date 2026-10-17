#!/usr/bin/env python3
"""move_snipr_runtime.py - move SNIPR's working files out of /tmp into /run/snipr.

Why: /tmp is world-writable (anyone on the box can pre-create or swap the files
SNIPR trusts) and the frames are rewritten every few seconds. /run/snipr is a
RAM-backed folder owned by the login user, recreated at every boot, so there is no
SD-card wear and nothing lingers after a power cycle.

    python3 move_snipr_runtime.py --check  FILE [FILE ...]   show what would change
    python3 move_snipr_runtime.py          FILE [FILE ...]   make the change
    python3 move_snipr_runtime.py --revert FILE [FILE ...]   put /tmp back
    sudo python3 move_snipr_runtime.py --tmpfiles            create /run/snipr now and at every boot

Each FILE is recognised by its content, so the same command works for any of:
  * ~/SNIPR/alpha/snipr_start_loop.sh, snipr_stop_loop.sh     (Alpha loop scripts)
  * ~/SNIPR/bravo/snipr_republish.sh and /opt/nucleus/snipr/snipr_republish.sh
  * a nucleusd cot_bridge.py that has the SNIPR block         (the received-frame path)
  * ~/apply_snipr_relay.py                                    (so a re-apply keeps the new path)

Edits are exact-text swaps, each of which must match exactly once; if anything
does not match, that file is left untouched. Files inside a git work tree get no
backup (git is the undo); other files get FILE.bak-pre-runpath. Nothing is
restarted here and nothing is transmitted.
"""
import argparse
import os
import shutil
import subprocess
import sys

RUN = "/run/snipr"
TMPFILES_PATH = "/etc/tmpfiles.d/snipr.conf"

GUARD = (
    '[[ -d "$SNIPR_RUN_DIR" && -w "$SNIPR_RUN_DIR" ]] || {\n'
    '    echo "SNIPR: $SNIPR_RUN_DIR is missing or not writable '
    '(run: sudo systemd-tmpfiles --create /etc/tmpfiles.d/snipr.conf)" >&2\n'
    '    exit 1\n'
    '}\n\n'
)

# kind -> list of (old, new). Each `old` must occur exactly once when applying;
# each `new` must occur exactly once when reverting.
RULES = {
    "loop": [
        ('PID_FILE="/tmp/snipr_loop.pid"',
         'SNIPR_RUN_DIR="${SNIPR_RUN_DIR:-/run/snipr}"   # RAM-backed; created at boot by /etc/tmpfiles.d/snipr.conf\n'
         'PID_FILE="$SNIPR_RUN_DIR/snipr_loop.pid"'),
        ('LOG_FILE="/tmp/snipr_loop.log"',
         'LOG_FILE="$SNIPR_RUN_DIR/snipr_loop.log"'),
    ],
    "loop_start_extra": [
        ('RAW_CAPTURE="/tmp/snipr_raw_capture.jpg"',
         'RAW_CAPTURE="$SNIPR_RUN_DIR/snipr_raw_capture.jpg"'),
        ('COMPRESSED_FRAME="/tmp/snipr_compressed_frame.jpg"',
         'COMPRESSED_FRAME="$SNIPR_RUN_DIR/snipr_compressed_frame.jpg"'),
        ("NOT numbered per-cycle, so /tmp doesn't",
         "NOT numbered per-cycle, so the run folder doesn't"),
        ('echo $$ > "$PID_FILE"',
         GUARD + 'echo $$ > "$PID_FILE"'),
    ],
    "republish": [
        ('LATEST_FRAME="/tmp/snipr_latest.jpg"',
         'LATEST_FRAME="${SNIPR_RUN_DIR:-/run/snipr}/snipr_latest.jpg"'),
    ],
    "bridge": [
        ('SNIPR_LATEST_FRAME_PATH = "/tmp/snipr_latest.jpg"',
         'SNIPR_LATEST_FRAME_PATH = "/run/snipr/snipr_latest.jpg"'),
    ],
}


def classify(text):
    """Return the list of rule groups that apply to this file, or []"""
    groups = []
    probes = {
        "loop": ('PID_FILE="/tmp/snipr_loop.pid"', 'SNIPR_RUN_DIR="${SNIPR_RUN_DIR:-/run/snipr}"'),
        "republish": ('LATEST_FRAME="/tmp/snipr_latest.jpg"', 'LATEST_FRAME="${SNIPR_RUN_DIR:-/run/snipr}/snipr_latest.jpg"'),
        "bridge": ('SNIPR_LATEST_FRAME_PATH = "/tmp/snipr_latest.jpg"', 'SNIPR_LATEST_FRAME_PATH = "/run/snipr/snipr_latest.jpg"'),
    }
    for g, (old, new) in probes.items():
        if old in text or new in text:
            groups.append(g)
    if "loop" in groups and 'RAW_CAPTURE=' in text:
        groups.append("loop_start_extra")
    return groups


def in_git(path):
    try:
        r = subprocess.run(["git", "-C", os.path.dirname(os.path.abspath(path)),
                            "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except FileNotFoundError:
        return False


def plan(text, revert):
    """Return (new_text, problems, changed_flag). Never partially applies."""
    groups = classify(text)
    if not groups:
        return text, ["no SNIPR /tmp or /run/snipr path found in this file"], False
    problems = []
    new_text = text
    changed = False
    for g in groups:
        for old, new in RULES[g]:
            src, dst = (new, old) if revert else (old, new)
            if not revert and dst in new_text:
                continue   # wanted state already present (checked first: `old` can be a substring of `new`)
            n = new_text.count(src)
            if n == 1:
                new_text = new_text.replace(src, dst)
                changed = True
            elif n == 0:
                if revert and dst in new_text:
                    continue   # already back on /tmp
                problems.append(f"expected text not found: {src.splitlines()[0][:70]!r}")
            else:
                problems.append(f"text appears {n} times (expected once): {src.splitlines()[0][:70]!r}")
    return new_text, problems, changed


def do_tmpfiles(check):
    owner = os.environ.get("SUDO_USER") or ("<login user>" if check else None)
    if not owner:
        print("STOP: run it with sudo from your login user (the folder is owned by that user).")
        return 1
    line = f"d {RUN} 0755 {owner} {owner} -\n"
    cur = open(TMPFILES_PATH).read() if os.path.exists(TMPFILES_PATH) else None
    print(f"   tmpfiles entry: {line.strip()}")
    if cur == line and os.path.isdir(RUN):
        print(f"   already in place; {RUN} exists.")
        return 0
    if check:
        print(f"   --check: would write {TMPFILES_PATH} and create {RUN}")
        return 0
    if os.geteuid() != 0:
        print("STOP: --tmpfiles needs sudo.")
        return 1
    with open(TMPFILES_PATH, "w") as f:
        f.write(line)
    os.chmod(TMPFILES_PATH, 0o644)
    r = subprocess.run(["systemd-tmpfiles", "--create", TMPFILES_PATH])
    if r.returncode != 0 or not os.path.isdir(RUN):
        print("STOP: systemd-tmpfiles did not create the folder.")
        return 1
    st = os.stat(RUN)
    print(f"   created {RUN} (mode {oct(st.st_mode & 0o777)}, owner uid {st.st_uid}); recreated at every boot.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--tmpfiles", action="store_true", help="create /run/snipr (needs sudo)")
    a = ap.parse_args()

    print(f"== SNIPR runtime folder: {'revert' if a.revert else 'move'} ({'check' if a.check else 'apply'}) ==")
    if a.tmpfiles:
        sys.exit(do_tmpfiles(a.check))
    if not a.files:
        print("STOP: give at least one FILE (or --tmpfiles).")
        sys.exit(1)

    plans = []
    bad = False
    for path in a.files:
        path = os.path.expanduser(path)
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as e:
            print(f"   {path}\n      STOP: cannot read ({e})")
            bad = True
            continue
        new_text, problems, changed = plan(text, a.revert)
        if problems:
            print(f"   {path}")
            for p in problems:
                print(f"      PROBLEM: {p}")
            bad = True
        elif not changed:
            print(f"   {path}\n      already {'on /tmp' if a.revert else 'moved'}; nothing to do")
        else:
            print(f"   {path}\n      will change ("
                  f"{'git is the undo' if in_git(path) else 'backup .bak-pre-runpath'})")
            plans.append((path, text, new_text))
    if bad:
        print("\nSTOP: nothing was written (fix the problems above first).")
        sys.exit(1)
    if a.check:
        print("\n--check: nothing was written.")
        return
    for path, text, new_text in plans:
        if not in_git(path):
            shutil.copy2(path, path + ".bak-pre-runpath")
        with open(path, "w", encoding="utf-8") as f:   # in place: keeps owner and mode
            f.write(new_text)
        print(f"   written: {path}")
    if plans:
        print("\nDone. Nothing was restarted. Restart what uses the changed files "
              "(cot-bridge for cot_bridge.py, snipr-republish for the republisher).")


if __name__ == "__main__":
    main()
