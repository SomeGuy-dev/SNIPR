#!/usr/bin/env python3
"""
snipr_compress.py — Phase 1 standalone compression pipeline

Implements the validated processing order from the project plan (§3.2):
  1. Crop dead space
  2. Resize to 160x120
  3. Burn in timestamp overlay
  4. Compress to JPEG, stepping quality DOWN until under the size ceiling

This is a standalone test/dev tool — no LoRa, no chunking, no radio.
It exists to validate capture->bytes reliability against real footage
before any networking code is built on top of it.

Usage:
    python3 snipr_compress.py <input_image> [--ceiling-bytes 3400] [--out out.jpg]
"""

import argparse
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFile

# Some sources (partial writes, truncated captures) can otherwise hard-fail
# on load. Tolerate it rather than crashing the whole pipeline on one bad frame.
ImageFile.LOAD_TRUNCATED_IMAGES = True

TARGET_SIZE = (160, 120)
DEFAULT_CEILING_BYTES = 3400   # matches the validated 15-packet baseline (§2.1)
# Real hard ceiling confirmed from the installed meshtastic library on real
# hardware 2026-08-07 (not the 237 borrowed from cot_bridge.py's LORA_MTU,
# which was wrong and caused every full-size chunk to be rejected in production):
#   python3 -c "from meshtastic.protobuf import mesh_pb2; print(mesh_pb2.Constants.DATA_PAYLOAD_LEN)"
MESHTASTIC_DATA_PAYLOAD_LEN = 228
CHUNK_HEADER_BYTES = 5         # SNIPR session-aware header: >HBBB = 5 bytes
USABLE_PAYLOAD_PER_PACKET = MESHTASTIC_DATA_PAYLOAD_LEN - CHUNK_HEADER_BYTES

# (Quality is now found via binary search in compress_to_ceiling, not a fixed ladder —
# see that function for details.)


def autocrop_dead_space(img: Image.Image, threshold: int = 12) -> Image.Image:
    """
    Crops near-black borders (e.g. letterboxing) before resizing, so the
    downscale doesn't waste resolution budget on blank space.
    Conservative: only trims rows/columns that are almost entirely near-black.
    """
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
        # Guard against over-cropping a genuinely dark scene
        return img
    return img.crop((0, top, w, bottom + 1))


def add_timestamp_overlay(img: Image.Image, text: str) -> Image.Image:
    """
    Burns a timestamp into the pixels, AFTER resize (per §3.2 — text must be
    sized/placed for the final small frame, not shrunk along with everything else).
    Black background box behind light green text for legibility against any scene.
    """
    img = img.copy()
    draw = ImageDraw.Draw(img)
    w, h = img.size
    fontsize = max(8, int(h * 0.09))
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", fontsize
        )
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = max(2, int(fontsize * 0.25))
    x, y = pad, h - th - pad * 2

    draw.rectangle([x - pad, y - pad, x + tw + pad, y + th + pad], fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=(0, 255, 0))
    return img


def compress_to_ceiling(img: Image.Image, ceiling_bytes: int, out_path: Path):
    """
    Binary-searches JPEG quality (1-95) to find the HIGHEST quality that still
    fits under ceiling_bytes, rather than jumping through coarse fixed steps.
    This closes the gap the old fixed-ladder approach left on the table —
    e.g. landing at quality 90 instead of stopping at 85 with ~900 unused bytes.

    Tries full-color (4:4:4) chroma subsampling first, since it's a real,
    independent quality lever (better color fidelity at a modest byte cost)
    — falls back to standard 4:2:0 subsampling only if 4:4:4 can't fit even
    at the lowest usable quality.

    Returns (final_quality, final_size_bytes, packets_needed, subsampling_used)
    or that same tuple + True as a 5th element if nothing fit at all.
    """
    MIN_QUALITY = 10
    MAX_QUALITY = 95

    def encode(quality: int, subsampling: int) -> int:
        img.save(out_path, format="JPEG", quality=quality, subsampling=subsampling)
        return out_path.stat().st_size

    def binary_search(subsampling: int):
        """Returns (best_quality, best_size) or None if even MIN_QUALITY doesn't fit."""
        lo, hi = MIN_QUALITY, MAX_QUALITY
        best = None
        # Confirm the floor even fits before searching upward
        floor_size = encode(MIN_QUALITY, subsampling)
        if floor_size > ceiling_bytes:
            return None
        best = (MIN_QUALITY, floor_size)
        while lo <= hi:
            mid = (lo + hi) // 2
            size = encode(mid, subsampling)
            if size <= ceiling_bytes:
                best = (mid, size)
                lo = mid + 1   # try higher quality
            else:
                hi = mid - 1   # too big, go lower
        return best

    # Prefer full-color (4:4:4) first — better fidelity, worth the extra bytes
    # when the ceiling allows it.
    result = binary_search(subsampling=0)
    subsampling_label = "4:4:4 (full color)"

    if result is None:
        # Even lowest quality at 4:4:4 didn't fit — fall back to 4:2:0,
        # which is more byte-efficient at the same quality setting.
        result = binary_search(subsampling=2)
        subsampling_label = "4:2:0 (standard)"

    if result is None:
        # Nothing fit at all, even at the most efficient settings —
        # encode at the absolute floor and flag it clearly rather than crash.
        size = encode(MIN_QUALITY, 2)
        packets = -(-size // USABLE_PAYLOAD_PER_PACKET)
        return MIN_QUALITY, size, packets, "4:2:0 (standard)", True

    quality, size = result
    encode(quality, 0 if subsampling_label.startswith("4:4:4") else 2)  # ensure file on disk matches reported result
    packets = -(-size // USABLE_PAYLOAD_PER_PACKET)
    return quality, size, packets, subsampling_label


def main():
    parser = argparse.ArgumentParser(description="SNIPR Phase 1 compression pipeline")
    parser.add_argument("input_image", help="Path to a captured frame (from ffmpeg single-frame grab)")
    parser.add_argument("--ceiling-bytes", type=int, default=None,
                         help=f"Max output size in bytes (default: {DEFAULT_CEILING_BYTES}, matches §2.1 baseline). "
                              f"Ignored if --target-packets is set.")
    parser.add_argument("--target-packets", type=int, default=None,
                         help="Target packet count instead of a raw byte ceiling — more directly maps to "
                              "transmission time (§3.3.1). E.g. 15-17 packets ≈ 6-7s at the realistic "
                              "2-3 packets/sec sustained rate.")
    parser.add_argument("--out", default="snipr_output.jpg", help="Output JPEG path")
    parser.add_argument("--timestamp", default=None,
                         help="Override timestamp text (default: current time, HH:MM:SS)")
    args = parser.parse_args()

    in_path = Path(args.input_image)
    if not in_path.exists():
        print(f"ERROR: input image not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.out)
    timestamp_text = args.timestamp or f"AS OF {time.strftime('%H:%M:%S')}"

    if args.target_packets is not None:
        ceiling_bytes = args.target_packets * USABLE_PAYLOAD_PER_PACKET
        ceiling_source = f"--target-packets {args.target_packets}"
    else:
        ceiling_bytes = args.ceiling_bytes or DEFAULT_CEILING_BYTES
        ceiling_source = "--ceiling-bytes" if args.ceiling_bytes else "default"

    t0 = time.monotonic()

    img = Image.open(in_path).convert("RGB")
    img = autocrop_dead_space(img)
    img = img.resize(TARGET_SIZE, Image.LANCZOS)
    img = add_timestamp_overlay(img, timestamp_text)

    result = compress_to_ceiling(img, ceiling_bytes, out_path)
    over_budget = len(result) == 5
    quality, size, packets, subsampling_label = result[0], result[1], result[2], result[3]

    elapsed_ms = (time.monotonic() - t0) * 1000
    est_tx_low = packets / 3.0
    est_tx_high = packets / 2.0

    print(f"Output:          {out_path}")
    print(f"Ceiling source:  {ceiling_source} ({ceiling_bytes} bytes)")
    print(f"Final size:      {size} bytes")
    print(f"Quality used:    {quality}{' (LOWEST — did not meet ceiling)' if over_budget else ''}")
    print(f"Chroma mode:     {subsampling_label}")
    print(f"Unused headroom: {ceiling_bytes - size} bytes")
    print(f"Packets needed:  {packets} (at {USABLE_PAYLOAD_PER_PACKET}B usable payload/packet)")
    print(f"Est. TX time:    {est_tx_low:.1f}-{est_tx_high:.1f}s (at 2-3 packets/sec realistic sustained rate, §3.3.1)")
    print(f"Processing time: {elapsed_ms:.1f} ms")

    if over_budget:
        print("\nWARNING: scene did not fit under the ceiling even at lowest quality.", file=sys.stderr)
        print("This is exactly the kind of real-world case §3.2's size-ceiling design anticipates —", file=sys.stderr)
        print("log this scene as a data point, don't just discard the result.", file=sys.stderr)


if __name__ == "__main__":
    main()
