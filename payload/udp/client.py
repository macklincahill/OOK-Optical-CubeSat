import os
import socket
import struct
import sys
import time
import zlib

SERVER_ADDRESS = "10.0.0.2"
SERVER_PORT = 5010

CHUNK_SIZE = 1460         
SOCKET_TIMEOUT = 0.25          # seconds
MAX_RETRIES = 50

# Packet types
TYPE_DATA = 0
TYPE_EOF  = 1

# Header: type(1) seq(4) len(2) crc32(4)  => total 11 bytes
HDR_FMT = "!BIHI"
HDR_SIZE = struct.calcsize(HDR_FMT)

def recv_ack(sock):
    data, _ = sock.recvfrom(64)
    if not data:
        return None
    # ACK for data: b"A" + seq(4)
    if data[0:1] == b"A" and len(data) >= 5:
        (seq,) = struct.unpack("!I", data[1:5])
        return ("ACK", seq)
    # Final file checksum: b"F" + crc32(4)
    if data[0:1] == b"F" and len(data) >= 5:
        (crc,) = struct.unpack("!I", data[1:5])
        return ("FILECRC", crc)
    return None

def main():
    filename = sys.argv[1]
    filesize = os.path.getsize(filename)

    # Compute whole-file CRC32 locally (for end-to-end verification)
    file_crc = 0
    with open(filename, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            file_crc = zlib.crc32(b, file_crc) & 0xFFFFFFFF

    starttime = time.time()
    
    print(f"Sending: {filename} ({filesize} bytes)")
    print(f"Local file CRC32: 0x{file_crc:08X}")

    # Start connection
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(SOCKET_TIMEOUT)

        seq = 0
        sent_bytes = 0

        # Open the file
        with open(filename, "rb") as fh:
            while True:
                # Read a piece of the file according to the size of each chunk
                payload = fh.read(CHUNK_SIZE) 
                if not payload:
                    break
                
                # Construct the packet
                crc = zlib.crc32(payload) & 0xFFFFFFFF
                header = struct.pack(HDR_FMT, TYPE_DATA, seq, len(payload), crc)
                pkt = header + payload

                # Send packet
                sock.sendto(pkt, (SERVER_ADDRESS, SERVER_PORT))

                ## Resend if failed
                #retries = 0
                #while True:
                #    sock.sendto(pkt, (SERVER_ADDRESS, SERVER_PORT))
                #    try:
                #        ack = recv_ack(sock)
                #        if ack and ack[0] == "ACK" and ack[1] == seq:
                #            break  # good
                #    except socket.timeout:
                #        retries += 1
                #        if retries > MAX_RETRIES:
                #            raise RuntimeError(f"Too many retries on seq={seq}")
                #        continue

                sent_bytes += len(payload)
                if seq % 50 == 0:
                    print(f"Sent seq={seq}  progress={sent_bytes}/{filesize}", end="\r")
                seq += 1

        print(f"\nAll data packets sent. total packets={seq}")
        
        currenttime = time.time()
        elapsedtime = currenttime - starttime
        datarate = (filesize / elapsedtime)  / pow(10,6)

        print(f"\nTime elapsed: {elapsedtime}s")
        print(f"Filesize: {filesize}b")

        print(f"Average datarate: {datarate} Mb/s")

        # Send EOF (no payload)
        eof_header = struct.pack(HDR_FMT, TYPE_EOF, seq, 0, 0)
        retries = 0
        while True:
            sock.sendto(eof_header, (SERVER_ADDRESS, SERVER_PORT))
            try:
                msg = recv_ack(sock)
                if msg and msg[0] == "FILECRC":
                    remote_crc = msg[1]
                    print(f"Server file CRC32: 0x{remote_crc:08X}")
                    if remote_crc == file_crc:
                        print("End-to-end checksum MATCH.")
                    else:
                        print("Checksum MISMATCH.")
                    break
            except socket.timeout:
                retries += 1
                if retries > MAX_RETRIES:
                    raise RuntimeError("Too many retries waiting for final checksum.")
                continue

if __name__ == "__main__":
    main()

