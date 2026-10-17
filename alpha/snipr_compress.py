#!/usr/bin/env python3
"""
snipr_compress.py v2.1 - SNIPR Alpha frame compressor.

Drop-in replacement for v1: same arguments, same output file, same exit codes.
By default it still writes today's 160x120 JPEG. A second format, HEVC (or H.264)
at 256x192, is switched on by a one-line config file, so installing this file
changes nothing on the air until you flip that switch.

  Switch file:  /etc/nucleus/snipr_codec.conf        (override with SNIPR_CONF)
  Contents:     codec=hevc size=256x192 display=auto preset=medium packets=7
  Back to JPEG: codec=jpeg        (or delete the file)

Settings precedence: command line > environment (SNIPR_CODEC, SNIPR_SIZE,
SNIPR_DISPLAY, SNIPR_PRESET, SNIPR_PACKETS) > switch file > defaults (jpeg).

packets=N is a ceiling for the video formats only (N data packets of 223 bytes).
It is applied on top of the loop's --target-packets and the smaller of the two
wins, so the loop script does not need editing. It never touches JPEG frames,
including the JPEG sent when the video encoder fails.

Frame layout on the air
  JPEG   : a plain .jpg file (starts FF D8), exactly as before.
  HEVC / H.264 : 2 header bytes + the compressed picture.
      byte 0   0xC5  (marker; a JPEG never starts with it)
      byte 1   (display_aspect << 4) | template_id
               template_id 0/1 = parameter sets are inline (HEVC / H.264)
               template_id >= 2 = both ends already know the parameter sets
               for that codec+size (see TEMPLATES), so they are not sent.
  If the encoder on this machine produces parameter sets that do not match the
  built-in template byte for byte, the frame is sent with them inline (about
  80 bytes bigger). It never sends a frame the other end could misread.

If the video encoder is missing or fails, the frame is sent as JPEG instead.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

# ----------------------------------------------------------------- constants
TARGET_SIZE = (160, 120)           # JPEG path, unchanged from v1
DEFAULT_CEILING_BYTES = 3400
MESHTASTIC_DATA_PAYLOAD_LEN = 228
CHUNK_HEADER_BYTES = 5
USABLE_PAYLOAD_PER_PACKET = MESHTASTIC_DATA_PAYLOAD_LEN - CHUNK_HEADER_BYTES   # 223

CONF_PATH = os.environ.get("SNIPR_CONF", "/etc/nucleus/snipr_codec.conf")
STATE_PATH = os.environ.get("SNIPR_STATE", "/tmp/snipr_codec_state.json")
FFMPEG = os.environ.get("SNIPR_FFMPEG", "ffmpeg")
FFMPEG_TIMEOUT = 40

WIRE_MAGIC = 0xC5
START = b"\x00\x00\x00\x01"
# display aspect codes (header byte 1, high nibble). 0 = same shape as the coded picture.
ASPECTS = {0: None, 1: (16, 9), 2: (3, 2), 3: (21, 9)}
TID_INLINE = {"hevc": 0, "h264": 1}

# BEGIN-TEMPLATES
TEMPLATES = {
    2: ("hevc", 256, 192,
        "0000000140010c01ffff0408000003009fa800000300003cba02400000000142"
        "01010408000003009fa800000300003ca008080c165ba924cae6808000000300"
        "8000000c84000000014401c172b02240"),
    3: ("hevc", 256, 144,
        "0000000140010c01ffff0408000003009fa800000300003cba02400000000142"
        "01010408000003009fa800000300003ca0080809165ba924cae6808000000300"
        "8000000c84000000014401c172b02240"),
    4: ("h264", 256, 192,
        "000000016764100cacb820334200000300020000030064080000000168ee044b"
        "22c0"),
    5: ("h264", 256, 144,
        "000000016764100cacb820274200000300020000030064080000000168ee044b"
        "22c0"),
}
# END-TEMPLATES

CRF_MIN, CRF_MAX = 10.0, 51.0
CRF_SLOPE = -0.16                  # log2(bytes) per CRF step, typical for small stills
DEFAULT_CRF_GUESS = {"hevc": 34.0, "h264": 33.0}


# ----------------------------------------------------------------- v1 image steps
def autocrop_dead_space(img, threshold=12):
    """Trim near-black borders (letterboxing) before resizing. Unchanged from v1."""
    gray = img.convert("L")
    w, h = gray.size
    px = gray.load()

    def row_is_dark(y):
        dark = sum(1 for x in range(0, w, max(1, w // 64)) if px[x, y] < threshold)
        return dark > (w // max(1, w // 64)) * 0.95

    top = 0
    while top < h and row_is_dark(top):
        top += 1
    bottom = h - 1
    while bottom > top and row_is_dark(bottom):
        bottom -= 1
    if bottom - top < h * 0.5:
        return img
    return img.crop((0, top, w, bottom + 1))


def add_timestamp_overlay(img, text, x_squeeze=1.0):
    """
    Burn the capture time into the pixels after the resize (v1 behaviour).
    x_squeeze < 1 narrows the label so that it looks normal after the receiver
    stretches the picture back to its display shape (e.g. 0.75 for 4:3 -> 16:9).
    """
    img = img.copy()
    w, h = img.size
    fontsize = max(8, int(h * 0.09))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", fontsize)
    except OSError:
        font = ImageFont.load_default()

    probe = ImageDraw.Draw(img)
    bbox = probe.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = max(2, int(fontsize * 0.25))

    if abs(x_squeeze - 1.0) < 0.01:
        x, y = pad, h - th - pad * 2
        probe.rectangle([x - pad, y - pad, x + tw + pad, y + th + pad], fill=(0, 0, 0))
        probe.text((x, y), text, font=font, fill=(0, 255, 0))
        return img

    # draw the label at full width on its own tile, narrow the tile, paste it
    tile_w, tile_h = tw + pad * 2 + 1, th + pad * 2 + 1
    tile = Image.new("RGB", (tile_w, tile_h), (0, 0, 0))
    ImageDraw.Draw(tile).text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=(0, 255, 0))
    new_w = max(8, int(round(tile_w * x_squeeze)))
    tile = tile.resize((new_w, tile_h), Image.LANCZOS)
    img.paste(tile, (max(0, int(pad * x_squeeze)), max(0, h - tile_h - pad)))
    return img


def compress_to_ceiling(img, ceiling_bytes, out_path):
    """JPEG quality search, unchanged from v1. Returns (quality, size, packets, label[, True])."""
    MIN_QUALITY, MAX_QUALITY = 10, 95

    def encode(quality, subsampling):
        img.save(out_path, format="JPEG", quality=quality, subsampling=subsampling)
        return out_path.stat().st_size

    def binary_search(subsampling):
        lo, hi = MIN_QUALITY, MAX_QUALITY
        floor_size = encode(MIN_QUALITY, subsampling)
        if floor_size > ceiling_bytes:
            return None
        best = (MIN_QUALITY, floor_size)
        while lo <= hi:
            mid = (lo + hi) // 2
            size = encode(mid, subsampling)
            if size <= ceiling_bytes:
                best = (mid, size)
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    result = binary_search(0)
    label = "4:4:4 (full color)"
    if result is None:
        result = binary_search(2)
        label = "4:2:0 (standard)"
    if result is None:
        size = encode(MIN_QUALITY, 2)
        return MIN_QUALITY, size, -(-size // USABLE_PAYLOAD_PER_PACKET), "4:2:0 (standard)", True

    quality, size = result
    encode(quality, 0 if label.startswith("4:4:4") else 2)
    return quality, size, -(-size // USABLE_PAYLOAD_PER_PACKET), label


# ----------------------------------------------------------------- Annex-B helpers
def split_nals(stream):
    """Annex-B byte stream -> list of NAL units (start codes removed)."""
    idx, i = [], 0
    while True:
        j = stream.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        idx.append(j)
        i = j + 3
    nals = []
    for k, j in enumerate(idx):
        end = idx[k + 1] if k + 1 < len(idx) else len(stream)
        nal = stream[j + 3:end].rstrip(b"\x00")      # a NAL never ends in 0x00
        if nal:
            nals.append(nal)
    return nals


def nal_type(nal, codec):
    return ((nal[0] >> 1) & 0x3F) if codec == "hevc" else (nal[0] & 0x1F)


def is_param(nal, codec):
    return nal_type(nal, codec) in ((32, 33, 34) if codec == "hevc" else (7, 8))


def is_slice(nal, codec):
    t = nal_type(nal, codec)
    return (t < 32) if codec == "hevc" else (t in (1, 5))


def pack_wire(codec, w, h, stream, aspect_code):
    """Annex-B stream -> bytes to send. Returns (wire_bytes, template_id)."""
    nals = split_nals(stream)
    params = [n for n in nals if is_param(n, codec)]
    slices = [n for n in nals if is_slice(n, codec)]
    if not slices:
        raise ValueError("encoder produced no picture data")
    template = b"".join(START + n for n in params)
    tid = None
    for t, (c, tw, th, hexstr) in TEMPLATES.items():
        if c == codec and tw == w and th == h and bytes.fromhex(hexstr) == template:
            tid = t
            break
    if tid is not None and len(slices) == 1:
        body = slices[0]
    else:
        tid = TID_INLINE[codec]
        body = START.join(params + slices)           # decoder puts the first start code back
    return bytes([WIRE_MAGIC, (aspect_code << 4) | tid]) + body, tid


# ----------------------------------------------------------------- video encode
def _stage(inp, codec, preset, crfs, tmpdir):
    """One ffmpeg process, one output per CRF value (single start-up cost)."""
    n = len(crfs)
    fc = ("[0:v]scale=out_color_matrix=bt601:out_range=tv:flags=accurate_rnd,format=yuv420p,"
          f"split={n}" + "".join(f"[s{i}]" for i in range(n)))
    cmd = [FFMPEG, "-v", "error", "-y", "-i", inp, "-filter_complex", fc]
    outs = {}
    for i, c in enumerate(crfs):
        o = os.path.join(tmpdir, f"o_{i}.{codec}")
        outs[c] = o
        if codec == "hevc":
            enc = ["-c:v", "libx265", "-preset", preset, "-x265-params",
                   f"crf={c:d}:keyint=1:wpp=0:log-level=none:no-info=1:repeat-headers=0:aud=0"]
        else:
            enc = ["-c:v", "libx264", "-preset", preset, "-crf", f"{c:d}", "-g", "1",
                   "-x264-params", "no-info=1:aud=0"]
        cmd += ["-map", f"[s{i}]"] + enc + ["-frames:v", "1", "-f", codec, o]
    subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return {c: open(o, "rb").read() for c, o in outs.items()}


def _load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {}


def _save_state(state):
    try:
        tmp = STATE_PATH + ".tmp"
        json.dump(state, open(tmp, "w"))
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def _next_crfs(results, budget):
    """
    Which integer CRFs to try next (an empty list = finished).
    x265 only honours whole CRF steps (each is about 8-10% in size), so we look for the
    adjacent pair where one step fits and the next lower step does not.
    """
    import math
    fits = sorted(c for c, b in results.items() if len(b) <= budget)
    lo_fit = fits[0] if fits else None
    overs = [c for c, b in results.items() if len(b) > budget and (lo_fit is None or c < lo_fit)]
    hi_over = max(overs) if overs else None
    target = math.log2(budget * 0.97)

    if lo_fit is not None and hi_over is not None:
        gap = list(range(hi_over + 1, lo_fit))
        if not gap:
            return []
        if len(gap) <= 3:
            return gap
        a, o = math.log2(len(results[lo_fit])), math.log2(len(results[hi_over]))
        frac = (o - target) / (o - a) if o > a else 0.5
        c = hi_over + frac * (lo_fit - hi_over)
        return [x for x in (round(c) - 1, round(c), round(c) + 1) if hi_over < x < lo_fit]
    if lo_fit is not None:                                   # everything fits: spend more bits
        if lo_fit <= CRF_MIN:
            return []
        c = lo_fit + (target - math.log2(len(results[lo_fit]))) / CRF_SLOPE
        cand = [x for x in (round(c) - 1, round(c), round(c) + 1) if CRF_MIN <= x < lo_fit]
        return cand or [lo_fit - 1]
    o = max(overs)                                           # nothing fits yet: need fewer bits (start from the closest miss)
    c = o + (target - math.log2(len(results[o]))) / CRF_SLOPE
    cand = [x for x in (round(c) - 1, round(c), round(c) + 1) if o < x <= CRF_MAX]
    return cand or [min(CRF_MAX, o + 3)]


def fit_video(small, codec, preset, budget, aspect_code):
    """
    Find the lowest whole CRF (best picture) whose wire size fits the budget.
    One ffmpeg run (three CRFs around the previous frame's answer) when the scene is steady,
    two or three runs after a big change. Returns dict(wire, tid, crf, stages, encodes).
    """
    w, h = small.size
    key = f"{codec}:{w}x{h}"
    state = _load_state()
    try:
        guess = int(round(float(state.get(key, DEFAULT_CRF_GUESS[codec]))))
    except (TypeError, ValueError):
        guess = int(DEFAULT_CRF_GUESS[codec])
    guess = min(46, max(20, guess))
    results = {}      # crf -> wire bytes
    tids = {}
    stages = 0
    encodes = 0

    with tempfile.TemporaryDirectory(prefix="snipr_enc_") as tmp:
        inp = os.path.join(tmp, "in.png")
        small.save(inp, format="PNG")

        def go(crfs):
            nonlocal stages, encodes
            crfs = sorted({int(min(CRF_MAX, max(CRF_MIN, c))) for c in crfs} - set(results))
            if not crfs:
                return False
            stages += 1
            encodes += len(crfs)
            for c, stream in _stage(inp, codec, preset, crfs, tmp).items():
                results[c], tids[c] = pack_wire(codec, w, h, stream, aspect_code)
            return True

        go([guess - 1, guess, guess + 1])
        for _ in range(4):
            nxt = _next_crfs(results, budget)
            if not nxt or not go(nxt):
                break
        tries = 0
        while not any(len(b) <= budget for b in results.values()) and tries < 4:
            tries += 1
            go([max(results) + 3 * tries])
        fits = [c for c, b in results.items() if len(b) <= budget]
        if not fits:
            raise RuntimeError("video encoder could not fit the byte budget")

    crf = min(fits)
    state[key] = crf
    _save_state(state)
    return dict(wire=results[crf], tid=tids[crf], crf=crf, stages=stages, encodes=encodes)


# ----------------------------------------------------------------- settings
def read_conf(path):
    cfg = {}
    try:
        for line in open(path):
            line = line.split("#", 1)[0]
            for tok in line.split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    cfg[k.strip().lower()] = v.strip()
    except OSError:
        pass
    return cfg


def parse_size(text):
    try:
        w, h = text.lower().split("x")
        w, h = int(w), int(h)
        if w % 8 or h % 8 or not (64 <= w <= 640) or not (64 <= h <= 480):
            raise ValueError
        return w, h
    except Exception:
        raise SystemExit(f"bad size {text!r}: use WxH, both multiples of 8 (e.g. 256x192)")


def pick_aspect(src_w, src_h, coded_w, coded_h, mode):
    """Header code for the shape the receiver should display, or 0 for 'as coded'."""
    if mode == "native":
        return 0
    src = src_w / src_h
    coded = coded_w / coded_h
    if abs(coded / src - 1) < 0.04:
        return 0
    for code, ar in ASPECTS.items():
        if ar and abs(src / (ar[0] / ar[1]) - 1) < 0.04:
            return code
    return 0


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="SNIPR Alpha frame compressor (v2.1)")
    ap.add_argument("input_image", nargs="?")
    ap.add_argument("--ceiling-bytes", type=int, default=None)
    ap.add_argument("--target-packets", type=int, default=None)
    ap.add_argument("--out", default="snipr_output.jpg")
    ap.add_argument("--timestamp", default=None)
    ap.add_argument("--codec", choices=["jpeg", "hevc", "h264"], default=None)
    ap.add_argument("--size", default=None, help="coded size for hevc/h264, e.g. 256x192")
    ap.add_argument("--display", choices=["auto", "native"], default=None,
                    help="auto: tell the receiver the true shape of the source (16:9) so it can stretch the picture back")
    ap.add_argument("--preset", default=None, help="x265/x264 preset (default medium)")
    ap.add_argument("--print-template", action="store_true",
                    help="print the parameter sets of this machine's encoder as hex and exit (needs --codec, --size)")
    args = ap.parse_args()

    # Encoding is a short burst of CPU. Run it at low priority so it never competes
    # with meshtasticd / cot_bridge while the previous frame is still on the air.
    try:
        os.nice(10)
    except OSError:
        pass

    conf = read_conf(CONF_PATH)

    def setting(name, cli, env, default):
        if cli is not None:
            return cli, "command line"
        if os.environ.get(env):
            return os.environ[env], "environment"
        if name in conf:
            return conf[name], CONF_PATH
        return default, "default"

    codec, codec_src = setting("codec", args.codec, "SNIPR_CODEC", "jpeg")
    codec = codec.lower()
    if codec not in ("jpeg", "hevc", "h264"):
        print(f"WARNING: unknown codec {codec!r} in {codec_src}, using jpeg", file=sys.stderr)
        codec = "jpeg"
    size_txt, _ = setting("size", args.size, "SNIPR_SIZE", "256x192")
    display, _ = setting("display", args.display, "SNIPR_DISPLAY", "auto")
    preset, _ = setting("preset", args.preset, "SNIPR_PRESET", "medium")

    if args.target_packets is not None:
        ceiling_bytes = args.target_packets * USABLE_PAYLOAD_PER_PACKET
        ceiling_source = f"--target-packets {args.target_packets}"
    else:
        ceiling_bytes = args.ceiling_bytes or DEFAULT_CEILING_BYTES
        ceiling_source = "--ceiling-bytes" if args.ceiling_bytes else "default"

    # packets=N (switch file / SNIPR_PACKETS): ceiling for the video formats only.
    video_ceiling, video_ceiling_source = ceiling_bytes, ceiling_source
    pk_txt, pk_src = setting("packets", None, "SNIPR_PACKETS", "")
    if pk_txt:
        try:
            pk = int(pk_txt)
            if not 1 <= pk <= 40:
                raise ValueError
        except ValueError:
            print(f"WARNING: bad packets={pk_txt!r} in {pk_src}, ignored (use a whole number 1-40)", file=sys.stderr)
        else:
            if pk * USABLE_PAYLOAD_PER_PACKET < video_ceiling:
                video_ceiling = pk * USABLE_PAYLOAD_PER_PACKET
                video_ceiling_source = f"packets={pk} from {pk_src}"

    if args.print_template:
        if codec == "jpeg":
            sys.exit("--print-template needs --codec hevc|h264")
        cw, ch = parse_size(size_txt)
        flat = Image.new("RGB", (cw, ch), (90, 120, 150))
        with tempfile.TemporaryDirectory() as t:
            fl = os.path.join(t, "f.png")
            flat.save(fl)
            st = _stage(fl, codec, preset, [30], t)[30]
        params = [n for n in split_nals(st) if is_param(n, codec)]
        print(b"".join(START + n for n in params).hex())
        return

    if not args.input_image or not Path(args.input_image).exists():
        print(f"ERROR: input image not found: {args.input_image}", file=sys.stderr)
        sys.exit(1)
    in_path = Path(args.input_image)
    out_path = Path(args.out)
    timestamp_text = args.timestamp or f"AS OF {time.strftime('%H:%M:%S')}"

    t0 = time.monotonic()
    img = Image.open(in_path).convert("RGB")
    img = autocrop_dead_space(img)
    src_w, src_h = img.size

    used_video = False
    note = ""
    if codec != "jpeg":
        cw, ch = parse_size(size_txt)
        aspect_code = pick_aspect(src_w, src_h, cw, ch, display)
        ar = ASPECTS[aspect_code]
        x_squeeze = (cw / ch) / (ar[0] / ar[1]) if ar else 1.0
        small = img.resize((cw, ch), Image.LANCZOS)
        small = add_timestamp_overlay(small, timestamp_text, x_squeeze)
        t_enc = time.monotonic()
        try:
            r = fit_video(small, codec, preset, video_ceiling, aspect_code)
            tmp_out = str(out_path) + ".tmp"
            with open(tmp_out, "wb") as f:
                f.write(r["wire"])
            os.replace(tmp_out, out_path)
            used_video = True
        except Exception as e:  # noqa - never stall the loop: fall back to JPEG
            err = getattr(e, "stderr", b"") or b""
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
            note = f"video encode failed ({type(e).__name__}: {str(e)[:120]} {err[-200:]}) - sent JPEG instead"
            print("WARNING: " + note, file=sys.stderr)
        enc_ms = (time.monotonic() - t_enc) * 1000

    if used_video:
        size = len(r["wire"])
        packets = -(-size // USABLE_PAYLOAD_PER_PACKET)
        elapsed_ms = (time.monotonic() - t0) * 1000
        print(f"Output:          {out_path}")
        print(f"Format:          {codec.upper()} {cw}x{ch}, preset {preset}, CRF {r['crf']}, "
              f"display {'as coded' if not ar else '%d:%d' % ar}, "
              f"{'parameter sets from template ' + str(r['tid']) if r['tid'] >= 2 else 'parameter sets inline'}")
        print(f"Ceiling source:  {video_ceiling_source} ({video_ceiling} bytes)")
        print(f"Final size:      {size} bytes")
        print(f"Unused headroom: {video_ceiling - size} bytes")
        print(f"Packets needed:  {packets} (at {USABLE_PAYLOAD_PER_PACKET}B usable payload/packet)")
        print(f"Encode:         {r['encodes']} encodes in {r['stages']} ffmpeg run(s), {enc_ms:.0f} ms")
        print(f"Processing time: {elapsed_ms:.1f} ms")
        print(f"SNIPR_FRAME codec={codec} size={cw}x{ch} bytes={size} packets={packets} "
              f"crf={r['crf']:d} tid={r['tid']} enc_ms={enc_ms:.0f}")
        return

    # ---- JPEG path (today's behaviour)
    img = img.resize(TARGET_SIZE, Image.LANCZOS)
    img = add_timestamp_overlay(img, timestamp_text)
    result = compress_to_ceiling(img, ceiling_bytes, out_path)
    over_budget = len(result) == 5
    quality, size, packets, subsampling_label = result[0], result[1], result[2], result[3]
    elapsed_ms = (time.monotonic() - t0) * 1000

    print(f"Output:          {out_path}")
    print(f"Ceiling source:  {ceiling_source} ({ceiling_bytes} bytes)")
    print(f"Final size:      {size} bytes")
    print(f"Quality used:    {quality}{' (LOWEST - did not meet ceiling)' if over_budget else ''}")
    print(f"Chroma mode:     {subsampling_label}")
    print(f"Unused headroom: {ceiling_bytes - size} bytes")
    print(f"Packets needed:  {packets} (at {USABLE_PAYLOAD_PER_PACKET}B usable payload/packet)")
    print(f"Est. TX time:    {packets / 3.0:.1f}-{packets / 2.0:.1f}s (at 2-3 packets/sec realistic sustained rate, 3.3.1)")
    print(f"Processing time: {elapsed_ms:.1f} ms")
    print(f"SNIPR_FRAME codec=jpeg size={TARGET_SIZE[0]}x{TARGET_SIZE[1]} bytes={size} packets={packets} q={quality}"
          + (" fallback=1" if note else ""))
    if over_budget:
        print("\nWARNING: scene did not fit under the ceiling even at lowest quality.", file=sys.stderr)
        print("Log this scene as a data point, don't just discard the result.", file=sys.stderr)


if __name__ == "__main__":
    main()
