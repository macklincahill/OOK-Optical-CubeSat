#!/usr/bin/env python3
import hashlib
import socket
import struct
import zlib

LISTEN_ADDRESS = "0.0.0.0"   # bind all interfaces; change if you must
LISTEN_PORT = 5009

OUTFILE = "received_file.bin"

TYPE_DATA = 0
TYPE_FIN  = 1

HDR_FMT = "!BIHI"  # type(1) seq(4) len(2) crc32(4)
HDR_SIZE = struct.calcsize(HDR_FMT)

# Must match client setting
USE_PACKET_CRC32 = True

ACK_FMT = "!I"

def send_ack(sock, addr, seq: int):
    sock.sendto(b"A" + struct.pack(ACK_FMT, seq), addr)

def main():
    expected = 0
    buffer = {}  # seq -> payload bytes
    sha256 = hashlib.sha256()
    total_packets = None
    total_size = None
    announced_sha = None

    print(f"Listening UDP on {LISTEN_ADDRESS}:{LISTEN_PORT}")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((LISTEN_ADDRESS, LISTEN_PORT))

        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        except OSError:
            pass

        with open(OUTFILE, "wb") as fout:
            while True:
                data, addr = sock.recvfrom(65535)
                if len(data) < HDR_SIZE:
                    continue

                pkt_type, seq, payload_len, crc = struct.unpack(HDR_FMT, data[:HDR_SIZE])
                payload = data[HDR_SIZE:HDR_SIZE + payload_len]
                if len(payload) != payload_len:
                    continue

                if pkt_type == TYPE_DATA:
                    # Validate CRC (fast) if enabled
                    if USE_PACKET_CRC32:
                        calc = zlib.crc32(payload) & 0xFFFFFFFF
                        if calc != crc:
                            # Corrupted; do not ACK so sender retransmits
                            continue

                    # ACK immediately (even if duplicate/out-of-order)
                    send_ack(sock, addr, seq)

                    # Duplicate old packet
                    if seq < expected:
                        continue

                    # Store if new
                    if seq not in buffer:
                        buffer[seq] = payload

                    # Flush in-order run
                    while expected in buffer:
                        chunk = buffer.pop(expected)
                        fout.write(chunk)
                        sha256.update(chunk)
                        expected += 1

                    if expected % 500 == 0 and expected > 0:
                        print(f"received={expected} packets", end="\r")

                elif pkt_type == TYPE_FIN:
                    # FIN payload: total_packets(4) filesize(8) sha256(32)
                    if payload_len < (4 + 8 + 32):
                        continue
                    total_packets, total_size = struct.unpack("!IQ", payload[:12])
                    announced_sha = payload[12:44]

                    # If we're missing data, keep receiving; but ACKing FIN isn't defined here.
                    # The client only sends FIN after all data packets are ACKed, so in a healthy run:
                    # expected == total_packets.
                    if expected != total_packets:
                        # Not complete; ignore FIN (client will retry)
                        continue

                    fout.flush()
                    file_sha = sha256.digest()

                    match = 1 if (announced_sha == file_sha) else 0
                    sock.sendto(b"F" + bytes([match]) + file_sha, addr)

                    print("\nFIN received.")
                    print(f"Wrote: {OUTFILE}")
                    if total_size is not None:
                        print(f"Expected size: {total_size} bytes")
                    print(f"Packets received (in-order): {expected}/{total_packets}")
                    print(f"SHA-256: {file_sha.hex()}")
                    print("SHA-256 MATCH" if match else "SHA-256 MISMATCH")
                    break

if __name__ == "__main__":
    main()
