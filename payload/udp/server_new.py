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

    # Metrics
    first_rx_time = None
    last_rx_time = None
    received_packets = 0
    received_payload_bytes = 0
    lost_packets = 0
    missing_ranges = []  # list of (start, end) inclusive

    last_print = time.time()

    with open(out_path, "wb") as f:
        while True:
            pkt, addr = sock.recvfrom(65535)
            if len(pkt) < 4:
                continue

            seq = HDR.unpack_from(pkt, 0)[0]
            payload = pkt[4:]

            # Start timing on first non-EOF packet
            if first_rx_time is None and seq != EOF_SEQ:
                first_rx_time = time.time()

            if seq == EOF_SEQ:
                last_rx_time = time.time()

                # Write missing ranges log + compute lost_packets correctly
                with open(missing_path, "w") as mf:
                    mf.write(f"source={addr[0]}:{addr[1]}\n")
                    mf.write(f"expected_next_seq={expected_seq}\n")
                    mf.write("missing_ranges_inclusive:\n")
                    for a, b in missing_ranges:
                        mf.write(f"{a}-{b}\n")
                        lost_packets += (b - a + 1)
                break

            # Count packet only if it is "new" in the forward direction
            # (duplicates/out-of-order older packets are ignored and not counted)
            if seq >= expected_seq:
                received_packets += 1
                received_payload_bytes += len(payload)

            # If we got the expected packet: write it
            if seq == expected_seq:
                f.write(payload)
                expected_seq += 1

            # If we jumped forward: mark gap as missing, pad zeros, then write this payload
            elif seq > expected_seq:
                gap_start = expected_seq
                gap_end = seq - 1
                missing_ranges.append((gap_start, gap_end))

                pad_len = len(payload)
                if pad_len > 0:
                    f.write(b"\x00" * (pad_len * (seq - expected_seq)))

                f.write(payload)
                expected_seq = seq + 1

            # If seq < expected_seq: ignore
            else:
                pass

            now = time.time()
            if now - last_print >= PRINT_EVERY_SEC:
                # Instantaneous stats snapshot
                elapsed = (now - first_rx_time) if first_rx_time else 0.0
                mbps = (received_payload_bytes * 8 / elapsed / 1e6) if elapsed > 0 else 0.0
                print(f"[RX] expected_seq={expected_seq}  rx_bytes={received_payload_bytes}  avg_Mbps={mbps:.2f}")
                last_print = now

    # Final metrics
    duration = (last_rx_time - first_rx_time) if (first_rx_time and last_rx_time) else 0.0
    avg_mbps = (received_payload_bytes * 8 / duration / 1e6) if duration > 0 else 0.0

    total_packets_expected = received_packets + lost_packets
    loss_pct = (lost_packets / total_packets_expected * 100.0) if total_packets_expected > 0 else 0.0

    print(f"[RX DONE] wrote file to {out_path}")
    print(f"[RX DONE] missing ranges logged to {missing_path}")
    print(f"[RX DONE] duration_s: {duration:.3f}")
    print(f"[RX DONE] received_packets: {received_packets}")
    print(f"[RX DONE] lost_packets: {lost_packets}")
    print(f"[RX DONE] packet_loss_percent: {loss_pct:.3f}%")
    print(f"[RX DONE] received_payload_bytes: {received_payload_bytes}")
    print(f"[RX DONE] average_goodput_Mbps: {avg_mbps:.2f}")

if __name__ == "__main__":
    main()
