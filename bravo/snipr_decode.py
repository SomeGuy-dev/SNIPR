#!/usr/bin/env python3
"""
snipr_decode.py - SNIPR Bravo: turn a reassembled frame into a JPEG for the republisher.

    snipr_decode.py IN OUT [--canvas 640x480]
    snipr_decode.py IN --info

IN is /tmp/snipr_latest.jpg as written by the reassembler. Two formats:
  * a plain JPEG (starts FF D8): copied to OUT unchanged (today's behaviour);
  * an HEVC / H.264 frame (starts 0xC5, see snipr_compress.py on Alpha): decoded
    with ffmpeg, stretched to its display shape and centred on a fixed canvas
    (black bars if the shapes differ), written to OUT as a high-quality JPEG.

The canvas is fixed so that the stream ATAK sees never changes size, whatever
format a given frame used. Standard library only; needs ffmpeg.

Exit codes: 0 ok, 3 unknown or damaged format, 4 decoder failed.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time

WIRE_MAGIC = 0xC5
START = b"\x00\x00\x00\x01"
ASPECTS = {0: None, 1: (16, 9), 2: (3, 2), 3: (21, 9)}
INLINE_CODEC = {0: "hevc", 1: "h264"}
FFMPEG = os.environ.get("SNIPR_FFMPEG", "ffmpeg")

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


def unwrap(data):
    """-> (codec, annexb_stream, aspect_code, template_id)"""
    if len(data) < 8 or data[0] != WIRE_MAGIC:
        raise ValueError("not a SNIPR video frame")
    aspect, tid = data[1] >> 4, data[1] & 0x0F
    body = data[2:]
    if tid in INLINE_CODEC:
        return INLINE_CODEC[tid], START + body, aspect, tid
    if tid in TEMPLATES:
        codec, _w, _h, hexstr = TEMPLATES[tid]
        return codec, bytes.fromhex(hexstr) + START + body, aspect, tid
    raise ValueError(f"unknown template id {tid} (this decoder is older than the sender)")


def fit_box(ar, cw, ch):
    """Largest (w, h) of aspect ar=(n, d) inside the canvas, even numbers."""
    n, d = ar
    if cw * d <= ch * n:          # canvas is narrower than the picture: width-limited
        w, h = cw, cw * d / n
    else:
        w, h = ch * n / d, ch
    return int(w) // 2 * 2, int(h) // 2 * 2


def write_atomic(path, data):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def decode_video(codec, stream, aspect, canvas):
    cw, ch = canvas
    ar = ASPECTS.get(aspect)
    if ar:
        bw, bh = fit_box(ar, cw, ch)
        scale = f"scale={bw}:{bh}"
    else:
        scale = f"scale={cw}:{ch}:force_original_aspect_ratio=decrease"
    vf = (f"{scale}:flags=lanczos+accurate_rnd:in_range=tv:in_color_matrix=bt601:out_color_matrix=bt601,"
          f"format=yuvj420p,pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:black")
    with tempfile.TemporaryDirectory(prefix="snipr_dec_") as tmp:
        src = os.path.join(tmp, "in." + codec)
        dst = os.path.join(tmp, "out.jpg")
        with open(src, "wb") as f:
            f.write(stream)
        cmd = [FFMPEG, "-v", "error", "-y", "-f", codec, "-i", src, "-frames:v", "1",
               "-vf", vf, "-q:v", "2", "-f", "mjpeg", dst]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 100:
            raise RuntimeError("ffmpeg: " + r.stderr.decode("utf-8", "replace").strip()[-300:])
        return open(dst, "rb").read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("outfile", nargs="?")
    ap.add_argument("--canvas", default=os.environ.get("SNIPR_CANVAS", "640x480"))
    ap.add_argument("--info", action="store_true", help="describe the frame and exit")
    args = ap.parse_args()

    try:
        data = open(args.infile, "rb").read()
    except OSError as e:
        print(f"cannot read {args.infile}: {e}", file=sys.stderr)
        return 3

    if data[:2] == b"\xff\xd8":
        if args.info:
            print(f"jpeg {len(data)} B")
            return 0
        if not args.outfile:
            ap.error("outfile required")
        write_atomic(args.outfile, data)
        print(f"jpeg passthrough {len(data)} B")
        return 0

    try:
        codec, stream, aspect, tid = unwrap(data)
    except ValueError as e:
        print(f"unusable frame ({len(data)} B): {e}", file=sys.stderr)
        return 3
    if args.info:
        ar = ASPECTS.get(aspect)
        print(f"{codec} template={tid} aspect={'%d:%d' % ar if ar else 'as coded'} {len(data)} B")
        return 0
    if not args.outfile:
        ap.error("outfile required")
    try:
        cw, ch = (int(x) for x in args.canvas.lower().split("x"))
    except ValueError:
        ap.error("--canvas must look like 640x480")

    t0 = time.monotonic()
    try:
        jpg = decode_video(codec, stream, aspect, (cw, ch))
    except Exception as e:  # noqa
        print(f"decode failed ({codec}, {len(data)} B): {e}", file=sys.stderr)
        return 4
    write_atomic(args.outfile, jpg)
    ar = ASPECTS.get(aspect)
    print(f"decoded {codec} template={tid} aspect={'%d:%d' % ar if ar else 'as coded'} "
          f"{len(data)} B -> {len(jpg)} B JPEG {cw}x{ch} in {(time.monotonic() - t0) * 1000:.0f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
