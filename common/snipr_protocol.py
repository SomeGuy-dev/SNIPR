"""
snipr_protocol.py — SNIPR image chunking/reassembly protocol.

Mirrors VLoRa's existing pattern in cot_bridge.py:
  - Small fixed header per chunk (VLoRa: struct.pack(">BH", payload_size, seq) = 3 bytes)
  - SNIPR: struct.pack(">BBB", frame_id, seq, total_chunks) = 3 bytes — same overhead
  - frame_id (not needed by VLoRa's continuous audio stream) lets the reassembler
    distinguish which frame a chunk belongs to, since SNIPR sends discrete images,
    not one continuous stream.

This module is transport-agnostic — it has no knowledge of LoRa, UDP, or radios.
It only turns bytes into chunks and chunks back into bytes. That's deliberate:
it's the piece that can be fully unit-tested without any hardware, per the
Phase 2 bar established in the project plan (byte-exact reconstruction, or a
clean discard — never a silently corrupted partial image).
"""

import struct
import time

HEADER_FORMAT = ">HBBB"          # session_id, frame_id, seq, total_chunks
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
# Real hard ceiling confirmed from the actual installed meshtastic library
# (not cot_bridge.py's LORA_MTU=237, which turned out to be wrong for this
# purpose — 237 caused every full-size chunk to be rejected by sendData()
# with "Data payload too big", confirmed on real hardware 2026-08-07):
#   python3 -c "from meshtastic.protobuf import mesh_pb2; print(mesh_pb2.Constants.DATA_PAYLOAD_LEN)"
#   -> 233
# This is the true max size of the *entire* value passed to iface.sendData()
# (our 3-byte SNIPR header + JPEG payload slice combined), not just the payload.
MESHTASTIC_DATA_PAYLOAD_LEN = 228
MAX_CHUNK_PAYLOAD = MESHTASTIC_DATA_PAYLOAD_LEN - HEADER_SIZE   # 225 bytes
PARITY_SEQ = 255                 # reserved seq value for XOR parity packet

MAX_FRAME_ID = 255               # single byte, rolls over
MAX_CHUNKS_PER_FRAME = 255       # single byte — comfortably above the ~20-chunk range this project actually uses

REASSEMBLY_TIMEOUT_SECONDS = 60  # generous vs. the ~5-8s TX budget; a frame that
                                  # hasn't completed in this window never will


class FrameTooLargeError(Exception):
    """Raised if a frame would need more chunks than the header format can address."""
    pass


def chunk_frame(frame_bytes: bytes, session_id: int, frame_id: int) -> list[bytes]:
    """
    Splits a compressed JPEG into LoRa-sized chunks, each carrying a 3-byte header.

    frame_id should be a rolling counter (0-255) — the caller is responsible for
    incrementing it per capture cycle. Reusing a frame_id before an old frame with
    the same ID has either completed or timed out could cause the reassembler to
    mix chunks from two different frames; see the "stale frame ID reuse" test
    below for why this matters in practice, not just in theory.
    """
    if not (0 <= frame_id <= MAX_FRAME_ID):
        raise ValueError(f"frame_id must be 0-{MAX_FRAME_ID}, got {frame_id}")

    total_chunks = -(-len(frame_bytes) // MAX_CHUNK_PAYLOAD)  # ceil division
    if total_chunks > MAX_CHUNKS_PER_FRAME:
        raise FrameTooLargeError(
            f"Frame needs {total_chunks} chunks, header format only supports up to {MAX_CHUNKS_PER_FRAME}"
        )
    if total_chunks == 0:
        total_chunks = 1  # a zero-byte frame still gets one (empty) chunk, rather than none at all

    packets = []
    for seq in range(total_chunks):
        start = seq * MAX_CHUNK_PAYLOAD
        payload = frame_bytes[start:start + MAX_CHUNK_PAYLOAD]
        header = struct.pack(HEADER_FORMAT, session_id, frame_id, seq, total_chunks)
        packets.append(header + payload)
    return packets


def build_parity_packet(data_packets: list[bytes], session_id: int, frame_id: int) -> bytes:
    if not data_packets:
        raise ValueError("cannot build parity for empty packet list")

    parity = bytearray(MAX_CHUNK_PAYLOAD)
    final_chunk_len = 0

    for index, packet in enumerate(data_packets):
        if len(packet) < HEADER_SIZE:
            raise ValueError("malformed SNIPR data packet")

        sid, fid, seq, total_chunks = struct.unpack(HEADER_FORMAT, packet[:HEADER_SIZE])
        if sid != session_id or fid != frame_id:
            raise ValueError("mixed frame/session in parity input")

        payload = packet[HEADER_SIZE:]
        if index == len(data_packets) - 1:
            final_chunk_len = len(payload)

        for i, value in enumerate(payload):
            parity[i] ^= value

    header = struct.pack(
        HEADER_FORMAT,
        session_id,
        frame_id,
        PARITY_SEQ,
        final_chunk_len,
    )

    return header + bytes(parity)


class SniprReassembler:
    """
    Buffers incoming chunks by frame_id and reconstructs complete frames.

    Design choices, each tied to a specific failure mode from the Phase 2 test bar:
      - Storing chunks in a dict keyed by seq (not a list) makes duplicate delivery
        a no-op for free — a re-received chunk just overwrites the same dict entry.
      - Completion is checked by comparing len(chunks) to the total_chunks value
        carried in every chunk's own header — order of arrival never matters.
      - Each frame_id tracks its own first-seen timestamp, so one stalled frame
        can time out and get discarded without affecting any other in-flight frame.
      - Chunks for a frame_id that's already been completed-and-cleared, or that
        already timed out and was discarded, are treated as stale and dropped —
        this is what prevents a late chunk from a previous frame_id reuse from
        silently contaminating a newer frame using the same ID.
    """

    def __init__(self, timeout_seconds: float = REASSEMBLY_TIMEOUT_SECONDS):
        self.timeout_seconds = timeout_seconds
        self._buffers: dict[int, dict[int, bytes]] = {}
        self._first_seen: dict[int, float] = {}
        self._total_chunks: dict[int, int] = {}
        self._parity: dict[tuple[int, int], bytes] = {}
        self._final_chunk_len: dict[tuple[int, int], int] = {}
        self._fec_recovered: set[tuple[int, int]] = set()
        self._resolved_frame_ids: set[int] = set()  # completed OR timed-out — both are "done", don't reopen

    def _cleanup_frame(self, frame_key):
        self._buffers.pop(frame_key, None)
        self._first_seen.pop(frame_key, None)
        self._total_chunks.pop(frame_key, None)
        self._parity.pop(frame_key, None)
        self._final_chunk_len.pop(frame_key, None)

    def _finish_frame(self, frame_key):
        total = self._total_chunks[frame_key]
        ordered = [self._buffers[frame_key][i] for i in range(total)]
        result = b"".join(ordered)
        self._resolved_frame_ids.add(frame_key)
        self._cleanup_frame(frame_key)
        return result

    def _try_complete(self, frame_key):
        chunks = self._buffers.get(frame_key)
        total = self._total_chunks.get(frame_key)
        if chunks is None or total is None:
            return None

        if len(chunks) == total:
            return self._finish_frame(frame_key)

        if len(chunks) != total - 1:
            return None

        parity = self._parity.get(frame_key)
        if parity is None:
            return None

        missing = [seq for seq in range(total) if seq not in chunks]
        if len(missing) != 1:
            return None

        missing_seq = missing[0]
        recovered = bytearray(parity)

        for payload in chunks.values():
            for i, value in enumerate(payload):
                recovered[i] ^= value

        if missing_seq == total - 1:
            final_len = self._final_chunk_len.get(frame_key, 0)
            if not 1 <= final_len <= MAX_CHUNK_PAYLOAD:
                return None
            recovered_payload = bytes(recovered[:final_len])
        else:
            recovered_payload = bytes(recovered)

        chunks[missing_seq] = recovered_payload
        self._fec_recovered.add(frame_key)
        return self._finish_frame(frame_key)

    def ingest(self, packet: bytes, now: float | None = None) -> bytes | None:
        """
        Feed one received packet in. Returns the fully reassembled frame bytes
        if this packet completed it, otherwise None.
        """
        if now is None:
            now = time.monotonic()

        if len(packet) < HEADER_SIZE:
            return None  # malformed/truncated packet — silently ignored, not crashed on

        session_id, frame_id, seq, total_chunks = struct.unpack(HEADER_FORMAT, packet[:HEADER_SIZE])
        frame_key = (session_id, frame_id)
        payload = packet[HEADER_SIZE:]

        if frame_key in self._resolved_frame_ids:
            return None  # stale chunk for an already-finished or already-discarded frame — drop it

        if seq == PARITY_SEQ:
            if len(payload) != MAX_CHUNK_PAYLOAD:
                return None
            final_len = total_chunks
            if not 1 <= final_len <= MAX_CHUNK_PAYLOAD:
                return None
            self._parity[frame_key] = payload
            self._final_chunk_len[frame_key] = final_len
            if frame_key not in self._first_seen:
                self._first_seen[frame_key] = now
            return self._try_complete(frame_key)

        if frame_key not in self._buffers:
            self._buffers[frame_key] = {}
            self._first_seen[frame_key] = now
            self._total_chunks[frame_key] = total_chunks
        elif self._total_chunks[frame_key] != total_chunks:
            # A chunk claiming a different total_chunks count for a frame_id that's
            # already open means frame_id got reused before the OLD frame with this
            # ID resolved or timed out — most likely two different real frames whose
            # sizes happened to need different chunk counts. Treat this as: the old,
            # still-incomplete frame under this ID is abandoned (it was incomplete
            # anyway, so discarding it is correct per the "clean discard" bar, not
            # a new failure), and start fresh for what is clearly a new frame.
            self._buffers[frame_key] = {}
            self._first_seen[frame_key] = now
            self._total_chunks[frame_key] = total_chunks

        self._buffers[frame_key][seq] = payload  # dict overwrite = duplicate-safe

        return self._try_complete(frame_key)

    def was_fec_recovered(self, frame_key):
        return frame_key in self._fec_recovered

    def clear_fec_recovered(self, frame_key):
        self._fec_recovered.discard(frame_key)

    def sweep_timeouts(self, now: float | None = None) -> list[int]:
        """
        Discards any in-flight frame that's been incomplete longer than the timeout.
        Returns the list of frame_ids discarded this sweep — caller can log them.
        Must be called periodically by the host (cot_bridge.py's existing main
        loop already runs a periodic sweep for its own RX/TX tracking dicts —
        this fits the same pattern, see integration notes).
        """
        if now is None:
            now = time.monotonic()

        discarded = []
        for frame_key, first_seen in list(self._first_seen.items()):
            if now - first_seen > self.timeout_seconds:
                discarded.append(frame_key)
                self._resolved_frame_ids.add(frame_key)
                self._buffers.pop(frame_key, None)
                self._first_seen.pop(frame_key, None)
                self._total_chunks.pop(frame_key, None)
                self._parity.pop(frame_key, None)
                self._final_chunk_len.pop(frame_key, None)
                self._fec_recovered.discard(frame_key)
        return discarded

    def forget_resolved(self, older_than_seconds: float = 60.0):
        """
        The _resolved_frame_ids set grows forever otherwise. In practice frame_ids
        roll over every 256 frames, so this only needs periodic light pruning —
        exposed here so the host can call it on the same cadence as sweep_timeouts.
        Simple approach: since we don't timestamp resolved IDs individually, just
        cap the set size rather than track per-ID age.
        """
        if len(self._resolved_frame_ids) > 512:
            self._resolved_frame_ids.clear()
