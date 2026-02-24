import socket
import struct
import time
from pathlib import Path

# Bind + output
LISTEN_IP = "0.0.0.0"
PORT = 5009
OUT_FILE = "received.bin"

# Datagram format: 4-byte big-endian seq + payload
HDR = struct.Struct("!I")
EOF_SEQ = 0xFFFFFFFF  # end marker packet

# Performance knobs
SOCK_RCVBUF = 16 * 1024 * 1024  # 16MB
PRINT_EVERY_SEC = 2.0           # reduce console overhead

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((LISTEN_IP, PORT))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_RCVBUF)

    out_path = Path(OUT_FILE)
    missing_path = out_path.with_suffix(out_path.suffix + ".missing.txt")

    expected_seq = 0
    total_bytes = 0
    missing_ranges = []  # list of (start, end) inclusive
    num_packet_loss = 0

    last_print = time.time()

    with open(out_path, "wb") as f:
        while True:
            pkt, addr = sock.recvfrom(65535)
            if len(pkt) < 4:
                continue

            seq = HDR.unpack_from(pkt, 0)[0]
            payload = pkt[4:]

            if seq == EOF_SEQ:
                # Done: write missing ranges log
                with open(missing_path, "w") as mf:
                    mf.write(f"source={addr[0]}:{addr[1]}\n")
                    mf.write(f"expected_next_seq={expected_seq}\n")
                    mf.write("missing_ranges_inclusive:\n")
                    for a, b in missing_ranges:
                        num_packet_loss += 1
                        mf.write(f"{a}-{b}\n")
                break

            # If we got the expected packet: write it
            if seq == expected_seq:
                f.write(payload)
                total_bytes += len(payload)
                expected_seq += 1

            # If we jumped forward: mark gap as missing, pad zeros, then write this payload
            elif seq > expected_seq:
                gap_start = expected_seq
                gap_end = seq - 1
                missing_ranges.append((gap_start, gap_end))

                # Pad zeros for each missing packet *payload length*:
                # We don't know the real payload length of missing packets.
                # Using current payload length keeps alignment consistent for most fixed-chunk senders.
                pad_len = len(payload)
                if pad_len > 0:
                    f.write(b"\x00" * (pad_len * (seq - expected_seq)))
                    total_bytes += pad_len * (seq - expected_seq)

                # Now write current payload
                f.write(payload)
                total_bytes += len(payload)
                expected_seq = seq + 1

            # If seq < expected_seq (duplicate / re-ordered older packet):
            # Fastest behavior: ignore it. (We already padded/logged.)
            else:
                pass

            now = time.time()
            if now - last_print >= PRINT_EVERY_SEC:
                print(f"[RX] expected_seq={expected_seq}  bytes_written={total_bytes}")
                last_print = now

    print(f"[RX DONE] wrote {total_bytes} bytes to {out_path}")
    print(f"[RX DONE] missing ranges logged to {missing_path}")
    print(f"[RX DONE] packets lost: {num_packet_loss}")

if __name__ == "__main__":
    main()
