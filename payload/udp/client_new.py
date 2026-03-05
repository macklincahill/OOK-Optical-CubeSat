import socket
import struct
import sys
import time

SERVER_IP = "10.0.0.1"
PORT = 5010

CHUNK_SIZE = 1460
HDR = struct.Struct("!I")
EOF_SEQ = 0xFFFFFFFF

SOCK_SNDBUF = 16 * 1024 * 1024  # 16MB

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file_path> [server_ip] [port]")
        sys.exit(1)

    file_path = sys.argv[1]
    server_ip = sys.argv[2] if len(sys.argv) >= 3 else SERVER_IP
    port = int(sys.argv[3]) if len(sys.argv) >= 4 else PORT

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_SNDBUF)

    seq = 0
    sent_bytes = 0

    t0 = time.time()
    with open(file_path, "rb") as f:
        while True:
            data = f.read(CHUNK_SIZE)
            if not data:
                break
            pkt = HDR.pack(seq) + data
            sock.sendto(pkt, (server_ip, port))
            sent_bytes += len(data)
            seq += 1

    sock.sendto(HDR.pack(EOF_SEQ), (server_ip, port))
    t1 = time.time()

    dt = t1 - t0
    mbps = (sent_bytes * 8 / dt / 1e6) if dt > 0 else 0.0

    print(f"[TX DONE] sent_bytes: {sent_bytes}")
    print(f"[TX DONE] sent_packets: {seq}")
    print(f"[TX DONE] duration_s: {dt:.3f}")
    print(f"[TX DONE] average_send_Mbps: {mbps:.2f}")
    print(f"[TX DONE] to {server_ip}:{port}")

if __name__ == "__main__":
    main()
