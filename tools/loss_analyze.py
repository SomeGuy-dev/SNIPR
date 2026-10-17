#!/usr/bin/env python3
"""loss_analyze.py ALPHA_FILE BRAVO_FILE ALPHA_NODE_ID BRAVO_NODE_ID
Pairs Alpha's transmitted SNIPR packets with Bravo's receipts.
Inputs: the files written by loss_collect.sh (or raw `journalctl -u meshtasticd -o short-precise` text).
Node ids are the Meshtastic node numbers as meshtasticd logs them, e.g. 0x12345678
(the node id shown as !12345678 in the apps)."""
import re, sys, math, collections
if len(sys.argv) != 5 or not all(re.fullmatch(r'(0x)?[0-9a-fA-F]{8}', a) for a in sys.argv[3:5]):
    sys.exit(__doc__)
ALPHA, BRAVO = ['0x' + a.lower().replace('0x', '') for a in sys.argv[3:5]]
AIR = 0.201   # 252 B at SF7/250 kHz
ts = re.compile(r'^\w{3} +\d+ (\d\d):(\d\d):(\d\d\.\d+) ')
def T(m): return int(m[1])*3600+int(m[2])*60+float(m[3])
def read(path):
    out = []
    for l in open(path, errors='ignore'):
        m = ts.match(l)
        if m: out.append((T(m), l))
    return out
A, B = read(sys.argv[1]), read(sys.argv[2])
# ---- Alpha side
tx, enq, delays, relays_a, queue = [], {}, [], 0, []
cnt_a = []
for t, l in A:
    m = re.search(r'Started Tx \(id=(0x[0-9a-f]+) fr=(0x[0-9a-f]+) .*?HopLim=(\d)', l)
    if m:
        if m[2] == ALPHA and m[3] == '0': tx.append((t, m[1]))
        elif m[2] != ALPHA: relays_a += 1
    m = re.search(r'enqueue for send \(id=(0x[0-9a-f]+) fr=%s' % ALPHA, l)
    if m: enq[m[1]] = t
    m = re.search(r'Setting tx delay:(\d+)', l)
    if m: delays.append((t, int(m[1])))
    m = re.search(r'(\d+) packets remain in the TX queue', l)
    if m: queue.append((t, int(m[1])))
    m = re.search(r'txGood=(\d+),txRelay=(\d+),rxGood=(\d+),rxBad=(\d+)', l)
    if m: cnt_a.append((t, *map(int, m.groups())))
# ---- Bravo side
rx, garbled, btx, cnt_b = {}, [], [], []
for t, l in B:
    m = re.search(r'Lora RX \(id=(0x[0-9a-f]+) fr=%s .*?HopLim=0 ' % ALPHA, l)
    if m: rx[m[1]] = t
    m = re.search(r'Lora RX \(id=0x22006d00 fr=(0x[0-9a-f]+) to=%s' % ALPHA, l)
    if m: garbled.append((t, m[1]))
    m = re.search(r'Started Tx \(id=(0x[0-9a-f]+) fr=(0x[0-9a-f]+) .*?len=(\d+)', l)
    if m: btx.append((t, m[2], int(m[3])))
    m = re.search(r'txGood=(\d+),txRelay=(\d+),rxGood=(\d+),rxBad=(\d+)', l)
    if m: cnt_b.append((t, *map(int, m.groups())))
gar_ids = {g[1] for g in garbled}
if not tx: sys.exit('no Alpha SNIPR transmissions found in the Alpha file')
rows = []
for k, (t, i) in enumerate(tx):
    gap = (t - tx[k-1][0] - AIR) if k else None
    out = 'ok' if i in rx else ('garbled' if i in gar_ids else 'missing')
    rows.append((t, i, gap, out, (t - enq[i]) if i in enq else None))
n = len(rows); ok = sum(r[3] == 'ok' for r in rows)
print('Alpha sent %d SNIPR packets; Bravo received %d intact (%.0f%%), %d garbled, %d never seen -> loss %.0f%%' % (
    n, ok, 100*ok/n, sum(r[3]=='garbled' for r in rows), sum(r[3]=='missing' for r in rows), 100*(n-ok)/n))
print('\nOutcome by the idle gap Alpha left before the packet (start to start minus the 201 ms of air):')
bins = [('under 50 ms', lambda g: g is not None and g < .05), ('50 to 100 ms', lambda g: g is not None and .05 <= g < .10),
        ('100 to 250 ms', lambda g: g is not None and .10 <= g < .25), ('250 ms or more / first', lambda g: g is None or g >= .25)]
print('  %-24s %5s %7s %8s %8s %6s' % ('gap before packet', 'n', 'intact', 'garbled', 'missing', 'loss'))
for name, f in bins:
    s = [r for r in rows if f(r[2])]
    if s: print('  %-24s %5d %7d %8d %8d %5.0f%%' % (name, len(s), sum(r[3]=='ok' for r in s), sum(r[3]=='garbled' for r in s), sum(r[3]=='missing' for r in s), 100*(1-sum(r[3]=='ok' for r in s)/len(s))))
print('\nAlpha tx-delay holds ("Setting tx delay"): %d, lengths ms: %s' % (len(delays), [d[1] for d in delays][:20]))
w = [r[4] for r in rows if r[4] is not None]
if w: print('Alpha queue wait (enqueue to start): median %.2f s, max %.2f s; deepest queue seen: %d' % (sorted(w)[len(w)//2], max(w), max([q[1] for q in queue] or [0])))
print('Alpha transmissions that were relays of others: %d (vs %d of its own SNIPR packets)' % (relays_a, n))
span = (tx[-1][0] - tx[0][0]) or 1
print('Bravo transmissions in the window: %d (%d relays of Alpha, %d garbled-frame relays of 252 B)' % (
    len(btx), sum(1 for b in btx if b[1] == ALPHA), sum(1 for b in btx if b[2] >= 240 and b[1] != BRAVO)))
print('Garbled frames at Bravo (first 4 bytes missing): %d' % len(garbled))
for nm, c in (('Alpha', cnt_a), ('Bravo', cnt_b)):
    if len(c) >= 2:
        a, b = c[0], c[-1]
        print('%s counters over the window: txGood +%d, txRelay +%d, rxGood +%d, rxBad +%d' % (nm, b[1]-a[1], b[2]-a[2], b[3]-a[3], b[4]-a[4]))
print('\nPer packet:')
for r in rows[:60]:
    print('  %8.3f  gap %6s  wait %5s  %-8s %s' % (r[0] % 100000, ('%.0f ms' % (r[2]*1000)) if r[2] is not None else 'first', ('%.2f s' % r[4]) if r[4] is not None else '-', r[3], r[1]))
