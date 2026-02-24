#!/usr/bin/env python3
import hashlib
import os
import socket
import struct
import sys
import time
import zlib
from collections import deque

SERVER_ADDRESS = "10.0.0.2"
SERVER_PORT = 5009

# Keep UDP payload <= ~1200 bytes to reduce fragmentation risk on tunneled/encapsulated paths.
MAX_UDP_PAYLOAD = 1200

# Protocol
TYPE_DATA = 0
TYPE_FIN  = 1

# Header: type(1) seq(4) len(2) crc32(4)  => 11 bytes
HDR_FMT = "!BIHI"
HDR_SIZE = struct.calcsize(HDR_FMT)

PAYLOAD_SIZE = MAX_UDP_PAYLOAD - HDR_SIZE  # bytes of file data per packet

# Reliability / performance knobs
WINDOW_SIZE = 128              # in-flight packets
INITIAL_RTO = 0.30             # seconds (will adapt)
MAX_RTO = 2.0
MIN_RTO = 0.05
MAX_RETRIES = 200              # per packet

# Integrity knobs
USE_PACKET_CRC32 = True        # set False if you *really* want to disable per-packet checks

ACK_FMT = "!I"                 # seq
FIN_ACK_PREFIX = b"F"          # b"F" + match(1) + sha256(32)

def make_packet(pkt_type: int, seq: int, payload: bytes) -> bytes:
    if USE_PACKET_CRC32 and payload:
        crc = zlib.crc32(payload) & 0xFFFFFFFF
    else:
        crc = 0
    header = struct.pack(HDR_FMT, pkt_type, seq, len(payload), crc)
    return header + payload

def parse_ack(data: bytes):
    # ACK: b"A" + seq(4)
    if len(data) >= 1 + 4 and data[:1] == b"A":
        (seq,) = struct.unpack(ACK_FMT, data[1:5])
        return ("ACK", seq)
    # FIN-ACK: b"F" + match(1) + sha256(32)
    if len(data) >= 1 + 1 + 32 and data[:1] == FIN_ACK_PREFIX:
        match = data[1]
        remote_sha = data[2:34]
        return ("FINACK", match, remote_sha)
    return None

def iter_file_chunks(path: str, chunk_size: int):
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            yield b

def main():
    if len(sys.argv) < 2:
        print("Usage: client.py <file_to_send>")
        raise SystemExit(2)

    filename = sys.argv[1]
    filesize = os.path.getsize(filename)
    total_packets = (filesize + PAYLOAD_SIZE - 1) // PAYLOAD_SIZE

    # Compute SHA-256 while also queueing chunks for send (streaming)
    sha256 = hashlib.sha256()
    chunks = deque()
    for chunk in iter_file_chunks(filename, PAYLOAD_SIZE):
        sha256.update(chunk)
        chunks.append(chunk)
    file_sha = sha256.digest()

    print(f"Sending: {filename} ({filesize} bytes) -> {SERVER_ADDRESS}:{SERVER_PORT}")
    print(f"Packets: {total_packets}  payload_size={PAYLOAD_SIZE}  window={WINDOW_SIZE}")
    print(f"Per-packet CRC32: {'ON' if USE_PACKET_CRC32 else 'OFF'}")
    print(f"End-to-end SHA-256: {file_sha.hex()}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Bigger buffers help absorb bursts without drops on host side.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    except OSError:
        pass

    sock.settimeout(0.05)  # short poll; we manage RTO ourselves

    # Sender state
    base = 0
    next_seq = 0
    rto = INITIAL_RTO
    srtt = None
    rttvar = None

    # seq -> dict(packet=bytes, sent_ts=float, retries=int)
    in_flight = {}

    start = time.time()
    bytes_sent_payload = 0

    def send_seq(seq: int, pkt: bytes):
        nonlocal bytes_sent_payload
        sock.sendto(pkt, (SERVER_ADDRESS, SERVER_PORT))
        if seq < total_packets:
            # Payload bytes for rate reporting: header is overhead, ignore it
            bytes_sent_payload += len(pkt) - HDR_SIZE

    # Main loop: send data packets reliably
    while base < total_packets:
        # Fill window
        while next_seq < total_packets and (next_seq - base) < WINDOW_SIZE:
            payload = chunks[next_seq]  # already chunked
            pkt = make_packet(TYPE_DATA, next_seq, payload)
            in_flight[next_seq] = {"packet": pkt, "sent_ts": time.time(), "retries": 0}
            send_seq(next_seq, pkt)
            next_seq += 1

        # Receive ACKs (as many as are queued)
        while True:
            try:
                data, _ = sock.recvfrom(256)
            except socket.timeout:
                break
            msg = parse_ack(data)
            if not msg:
                continue
            if msg[0] == "ACK":
                ack_seq = msg[1]
                st = in_flight.get(ack_seq)
                if st:
                    # RTT measurement
                    now = time.time()
                    sample = max(0.0, now - st["sent_ts"])
                    # Jacobson/Karels style EWMA (RFC6298-ish)
                    if srtt is None:
                        srtt = sample
                        rttvar = sample / 2
                    else:
                        assert rttvar is not None
                        rttvar = 0.75 * rttvar + 0.25 * abs(srtt - sample)
                        srtt = 0.875 * srtt + 0.125 * sample
                    rto = min(MAX_RTO, max(MIN_RTO, (srtt + 4 * rttvar)))
                    del in_flight[ack_seq]

                    # Slide base forward
                    while base not in in_flight and base < next_seq:
                        base += 1

        # Retransmit timed-out packets
        now = time.time()
        for seq, st in list(in_flight.items()):
            if now - st["sent_ts"] >= rto:
                st["retries"] += 1
                if st["retries"] > MAX_RETRIES:
                    raise RuntimeError(f"Too many retries on seq={seq}")
                st["sent_ts"] = now
                send_seq(seq, st["packet"])

        if base % 200 == 0:
            progress = (base / total_packets) * 100 if total_packets else 100
            print(f"progress={progress:6.2f}%  base={base}/{total_packets}  rto={rto:.3f}s", end="\r")

    print("\nAll data packets ACKed.")

    # Send FIN containing metadata + file SHA-256
    # FIN payload: total_packets(4) filesize(8) sha256(32)
    fin_payload = struct.pack("!IQ", total_packets, filesize) + file_sha
    fin_pkt = make_packet(TYPE_FIN, total_packets, fin_payload)

    fin_retries = 0
    while True:
        sock.sendto(fin_pkt, (SERVER_ADDRESS, SERVER_PORT))
        try:
            data, _ = sock.recvfrom(512)
            msg = parse_ack(data)
            if msg and msg[0] == "FINACK":
                match, remote_sha = msg[1], msg[2]
                print(f"Server SHA-256: {remote_sha.hex()}")
                if match == 1 and remote_sha == file_sha:
                    print("Transfer complete: SHA-256 MATCH.")
                else:
                    print("Transfer finished but SHA-256 MISMATCH (file corrupted).")
                break
        except socket.timeout:
            fin_retries += 1
            if fin_retries > 100:
                raise RuntimeError("Too many retries waiting for FIN-ACK.")

    elapsed = time.time() - start
    mbps = (filesize * 8 / elapsed) / 1e6 if elapsed > 0 else 0.0
    print(f"Elapsed: {elapsed:.3f}s  Avg throughput: {mbps:.3f} Mb/s")

if __name__ == "__main__":
    main()
