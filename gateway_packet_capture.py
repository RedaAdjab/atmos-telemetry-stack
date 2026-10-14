"""Temporarily receive and inspect Semtech UDP gateway packets locally.

This does not forward packets to TTN. Configure the gateway's packet-forwarder
server address to this laptop while troubleshooting, then restore the TTN
server address afterwards.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("gateway_packet_capture")
SEMTECH_PROTOCOL_VERSION = 2
PUSH_DATA = 0x00
PUSH_ACK = 0x01
PULL_DATA = 0x02
PULL_ACK = 0x04
MAX_DATAGRAM_SIZE = 65535


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Receive and display Semtech UDP gateway packets locally."
    )
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        help="Local address to listen on (default: all interfaces).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=1700,
        help="UDP port used by the gateway packet forwarder (default: 1700).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSONL file for complete received packet payloads.",
    )
    parser.add_argument(
        "--no-ack",
        action="store_true",
        help="Do not send protocol acknowledgements to the gateway.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="Stop after this many PUSH_DATA packets (0 means keep running).",
    )
    return parser.parse_args()


def packet_kind(packet_type: int) -> str:
    return {
        PUSH_DATA: "PUSH_DATA",
        PUSH_ACK: "PUSH_ACK",
        PULL_DATA: "PULL_DATA",
        PULL_ACK: "PULL_ACK",
    }.get(packet_type, f"0x{packet_type:02x}")


def send_ack(
    sock: socket.socket, address: tuple[str, int], packet: bytes, packet_type: int
) -> None:
    if len(packet) < 4:
        return
    # Semtech UDP acknowledgements echo the protocol version and token.
    ack = bytes((packet[0], packet[1], packet[2], packet_type))
    sock.sendto(ack, address)


def save_payload(output: Any, received_at: str, sender: tuple[str, int], payload: Any) -> None:
    output.write(
        json.dumps(
            {
                "received_at": received_at,
                "sender": {"address": sender[0], "port": sender[1]},
                "payload": payload,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    output.flush()


def describe_uplink(received_at: str, sender: tuple[str, int], gateway_id: str, uplink: dict[str, Any]) -> str:
    timestamp = uplink.get("tmst", "?")
    frequency = uplink.get("freq", "?")
    data_rate = uplink.get("datr", "?")
    rssi = uplink.get("rssi", "?")
    size = uplink.get("size", "?")
    return (
        f"{received_at} PUSH_DATA from {sender[0]}:{sender[1]} "
        f"gateway={gateway_id} tmst={timestamp} freq={frequency} "
        f"data_rate={data_rate} rssi={rssi} size={size}"
    )


def handle_datagram(
    sock: socket.socket,
    packet: bytes,
    sender: tuple[str, int],
    output: Any,
    acknowledge: bool,
) -> bool:
    if len(packet) < 4:
        LOGGER.warning("Ignoring short datagram (%d bytes) from %s", len(packet), sender)
        return False

    received_at = datetime.now(timezone.utc).isoformat()
    packet_type = packet[3]
    kind = packet_kind(packet_type)

    if acknowledge and packet_type == PUSH_DATA:
        send_ack(sock, sender, packet, PUSH_ACK)
    elif acknowledge and packet_type == PULL_DATA:
        send_ack(sock, sender, packet, PULL_ACK)

    if packet_type not in (PUSH_DATA, PULL_DATA):
        LOGGER.info("%s from %s", kind, sender)
        return False

    if packet_type == PULL_DATA:
        LOGGER.info("%s from %s", kind, sender)
        return False

    if len(packet) < 12:
        LOGGER.warning("Ignoring short PUSH_DATA datagram (%d bytes) from %s", len(packet), sender)
        return False

    try:
        payload = json.loads(packet[12:].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        LOGGER.warning("Ignoring invalid PUSH_DATA JSON from %s: %s", sender, error)
        return False

    if not isinstance(payload, dict):
        LOGGER.warning("Ignoring PUSH_DATA with a non-object JSON payload from %s", sender)
        return False

    gateway_id = packet[4:12].hex()
    uplinks = payload.get("rxpk", [])
    if not isinstance(uplinks, list):
        LOGGER.warning("PUSH_DATA rxpk is not a list from gateway=%s", gateway_id)
        uplinks = []

    if not uplinks:
        LOGGER.info("%s from %s gateway=%s (no radio uplinks)", kind, sender, gateway_id)
    for uplink in uplinks:
        if isinstance(uplink, dict):
            LOGGER.info("%s", describe_uplink(received_at, sender, gateway_id, uplink))
        else:
            LOGGER.warning("Ignoring non-object radio packet from gateway=%s", gateway_id)

    if output is not None:
        save_payload(output, received_at, sender, payload)
    return True


def main() -> int:
    args = parse_args()
    if args.port < 1 or args.port > 65535:
        raise ValueError("--port must be between 1 and 65535")
    if args.count < 0:
        raise ValueError("--count cannot be negative")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    output = args.output.open("a", encoding="utf-8") if args.output else None
    received = 0
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((args.bind, args.port))
            LOGGER.info("Listening for Semtech UDP packets on %s:%d", args.bind, args.port)
            LOGGER.info("Press Ctrl+C to stop. This listener does not forward packets to TTN.")
            while args.count == 0 or received < args.count:
                packet, sender = sock.recvfrom(MAX_DATAGRAM_SIZE)
                if handle_datagram(sock, packet, sender, output, not args.no_ack):
                    received += 1
    except KeyboardInterrupt:
        LOGGER.info("Stopped after receiving %d PUSH_DATA packet(s)", received)
    finally:
        if output is not None:
            output.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        LOGGER.error("%s", error)
        sys.exit(1)
