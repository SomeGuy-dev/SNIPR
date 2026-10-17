#!/bin/bash
# Read-only: measure how busy Bravo is for SNIPR over SECS seconds (default 30). Changes nothing.
echo "=== $(date '+%F %T')  load: $(cut -d' ' -f1-3 /proc/loadavg) ==="
for q in measure_temp "measure_clock arm" get_throttled; do
  command -v vcgencmd >/dev/null 2>&1 && echo "$(vcgencmd $q 2>/dev/null)"
done
SECS="${SECS:-30}" python3 - <<'PY'
import glob, os, time

def snap():
    procs = {}
    for d in glob.glob('/proc/[0-9]*'):
        try:
            s = open(d + '/stat').read()
            r = s.rindex(')')
            name = s[s.index('(') + 1:r]
            f = s[r + 2:].split()
            procs[int(d[6:])] = (name, int(f[11]) + int(f[12]))      # user + system ticks
        except Exception:
            pass
    c = list(map(int, open('/proc/stat').readline().split()[1:9]))
    return procs, c

secs = float(os.environ.get('SECS', '30'))
tck = os.sysconf('SC_CLK_TCK')
ncpu = os.cpu_count() or 1
p0, c0 = snap()
t0 = time.time()
time.sleep(secs)
p1, c1 = snap()
dt = time.time() - t0
tot = sum(c1) - sum(c0)
idle = (c1[3] + c1[4]) - (c0[3] + c0[4])
busy_cores = (tot - idle) / tck / dt
by = {}
for pid, (name, t) in p1.items():
    if pid in p0 and p0[pid][0] == name:
        by[name] = by.get(name, 0) + (t - p0[pid][1])
rows = sorted(((v / tck / dt * 100, k) for k, v in by.items()), reverse=True)
seen = sum(v for v, _ in rows) / 100
print(f"over {dt:.0f} s on {ncpu} cores: {100 * (tot - idle) / tot:.1f}% busy overall = {busy_cores:.2f} of {ncpu} cores")
print("processes that ran the whole time (% of ONE core):")
for v, k in rows[:6]:
    print(f"  {v:5.1f}%  {k}")
print(f"everything else (short-lived decoders, helpers, the kernel): {max(busy_cores - seen, 0) * 100:.1f}% of one core")
PY
echo "--- decodes logged by the republisher, last 10 min ---"
sudo journalctl -u snipr-republish --since "10 min ago" --no-pager 2>/dev/null | grep -o 'in [0-9]* ms' | awk '{n++; s+=$2; if ($2>m) m=$2} END {if (n) printf "%d decodes, mean %.0f ms, longest %d ms\n", n, s/n, m; else print "none logged"}'
