"""
rdt30.py - Reliable Data Transfer 3.0 (Stop-and-Wait / Alternating Bit Protocol)
==================================================================================

Protocol behaviour
------------------
Sender
  • Divides the message into fixed-size chunks (packets).
  • Sends ONE packet at a time.
  • Starts a retransmit timer immediately after sending.
  • Waits for an ACK.
      – If the ACK arrives, is uncorrupted, and has the expected ack_num
        → advance to the next packet.
      – If the ACK is corrupt, or has the wrong ack_num, or the timer fires
        → retransmit the current packet.
  • Sequence numbers alternate: 0 → 1 → 0 → 1 → …

Receiver
  • Expects packet with seq_num == expected_seq.
      – If it arrives uncorrupted and with the right seq_num
        → deliver to upper layer, send ACK(seq), flip expected_seq.
      – Otherwise (corrupt or wrong seq)
        → send ACK for the *last correctly received* packet (or ACK for the
          opposite seq to signal "re-send what you just sent").

FSM states
----------
Sender: WAIT_FOR_CALL | WAIT_FOR_ACK
Receiver: WAIT_FOR_PKT_0 | WAIT_FOR_PKT_1
"""

import threading
import time
from network import Packet, NetworkChannel


# ──────────────────────────────────────────────────────────────────────────────
# FSM state labels (purely for logging)
# ──────────────────────────────────────────────────────────────────────────────
class SenderState:
    WAIT_CALL_0 = "WAIT_CALL_0"
    WAIT_ACK_0  = "WAIT_ACK_0"
    WAIT_CALL_1 = "WAIT_CALL_1"
    WAIT_ACK_1  = "WAIT_ACK_1"


class ReceiverState:
    WAIT_PKT_0 = "WAIT_PKT_0"
    WAIT_PKT_1 = "WAIT_PKT_1"


# ──────────────────────────────────────────────────────────────────────────────
class RDT30Sender:
    """
    rdt 3.0 sender — Stop-and-Wait.

    Parameters
    ----------
    channel        : NetworkChannel  – forward channel (sender → receiver)
    ack_channel    : NetworkChannel  – backward channel (receiver → sender)
    timeout        : float           – retransmission timeout in seconds
    packet_size    : int             – max bytes per packet payload
    verbose        : bool
    """

    def __init__(self, channel: NetworkChannel, ack_channel: NetworkChannel,
                 timeout: float = 1.0, packet_size: int = 8,
                 verbose: bool = True):

        self.channel     = channel
        self.ack_channel = ack_channel
        self.timeout     = timeout
        self.packet_size = packet_size
        self.verbose     = verbose

        # FSM bookkeeping
        self.seq        = 0                 # alternating bit: 0 or 1
        self.state      = SenderState.WAIT_CALL_0

        # Synchronisation primitives
        self._ack_event = threading.Event() # set when a valid ACK arrives
        self._lock      = threading.Lock()

        # Statistics
        self.stats = {"packets_sent": 0, "retransmissions": 0,
                      "acks_received": 0, "total_time": 0.0}

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def send(self, data: bytes) -> None:
        """Reliably transmit *data* byte-string to the receiver."""
        start = time.time()

        # Chop data into chunks
        chunks = [data[i:i + self.packet_size]
                  for i in range(0, len(data), self.packet_size)]

        self._log(f"\n[SENDER] Starting transfer: {len(chunks)} packet(s), "
                  f"{len(data)} bytes total")

        for idx, chunk in enumerate(chunks):
            self._send_packet(chunk, idx, len(chunks))

        self.stats["total_time"] = time.time() - start
        self._log(f"\n[SENDER] All {len(chunks)} packet(s) delivered successfully "
                  f"in {self.stats['total_time']:.3f}s")
        self._print_stats()

    # ──────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────

    def _send_packet(self, payload: bytes, idx: int, total: int) -> None:
        """Send a single chunk; retransmit until ACKed."""
        pkt = Packet(seq_num=self.seq, data=payload)

        while True:
            self._log(f"\n[SENDER] [{self.state}] Sending pkt {idx+1}/{total} "
                      f"(seq={self.seq}): {payload!r}")
            self._ack_event.clear()
            self.stats["packets_sent"] += 1

            # Push the packet into the (unreliable) network
            self.channel.transmit(pkt, self._on_ack_received)

            # Wait for ACK or timeout
            acked = self._ack_event.wait(timeout=self.timeout)

            if acked:
                # Flip the alternating bit and update FSM
                self.seq ^= 1
                self.state = (SenderState.WAIT_CALL_1
                              if self.seq == 1 else SenderState.WAIT_CALL_0)
                self._log(f"[SENDER] ACK received → advancing to seq={self.seq}")
                break
            else:
                self.stats["retransmissions"] += 1
                self._log(f"[SENDER] TIMEOUT — retransmitting pkt seq={self.seq}")

    def _on_ack_received(self, ack_pkt: Packet) -> None:
        """Called by the channel when an ACK packet arrives."""
        self.stats["acks_received"] += 1

        if ack_pkt.is_corrupt():
            self._log(f"[SENDER] Received CORRUPT ACK — ignoring")
            return

        if ack_pkt.ack_num != self.seq:
            self._log(f"[SENDER] Received ACK for wrong seq "
                      f"(got {ack_pkt.ack_num}, expected {self.seq}) — ignoring")
            return

        self._log(f"[SENDER] Received valid ACK(ack_num={ack_pkt.ack_num})")
        self._ack_event.set()

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def _print_stats(self):
        print("\n  ── Sender Statistics ────────────────────────────")
        for k, v in self.stats.items():
            print(f"     {k:20s}: {v}")
        print("  ────────────────────────────────────────────────")


# ──────────────────────────────────────────────────────────────────────────────
class RDT30Receiver:
    """
    rdt 3.0 receiver — Stop-and-Wait.

    Delivers received data to an internal buffer accessible via .received_data.
    """

    def __init__(self, ack_channel: NetworkChannel, verbose: bool = True):
        self.ack_channel   = ack_channel
        self.verbose       = verbose

        self.expected_seq  = 0
        self.state         = ReceiverState.WAIT_PKT_0
        self.received_data = bytearray()

        # Stats
        self.stats = {"pkts_received": 0, "pkts_delivered": 0,
                      "acks_sent": 0, "duplicates": 0, "corrupted": 0}

    # ──────────────────────────────────────────────────────────────────
    # Called by the channel when a DATA packet arrives
    # ──────────────────────────────────────────────────────────────────

    def receive(self, pkt: Packet) -> None:
        """Process an incoming data packet."""
        self.stats["pkts_received"] += 1
        self._log(f"\n[RECEIVER] [{self.state}] Got: {pkt}")

        # ── Case 1: Corrupted packet ──────────────────────────────────
        if pkt.is_corrupt():
            self.stats["corrupted"] += 1
            self._log(f"[RECEIVER] Packet CORRUPT — sending NAK "
                      f"(ACK for opposite seq={1 - self.expected_seq})")
            self._send_ack(1 - self.expected_seq)   # implicitly a NAK
            return

        # ── Case 2: Wrong sequence number (duplicate) ─────────────────
        if pkt.seq_num != self.expected_seq:
            self.stats["duplicates"] += 1
            self._log(f"[RECEIVER] Duplicate seq={pkt.seq_num} "
                      f"(expected {self.expected_seq}) — re-ACKing")
            self._send_ack(pkt.seq_num)              # ACK the duplicate
            return

        # ── Case 3: Expected packet ───────────────────────────────────
        self._log(f"[RECEIVER] Delivering seq={pkt.seq_num}: {pkt.data!r}")
        self.received_data.extend(pkt.data)
        self.stats["pkts_delivered"] += 1
        self._send_ack(self.expected_seq)

        # Flip expected sequence
        self.expected_seq ^= 1
        self.state = (ReceiverState.WAIT_PKT_1
                      if self.expected_seq == 1 else ReceiverState.WAIT_PKT_0)

    def _send_ack(self, ack_num: int) -> None:
        self.stats["acks_sent"] += 1
        ack = Packet(seq_num=0, is_ack=True, ack_num=ack_num)
        self.ack_channel.transmit(ack, lambda _: None)  # ACK sink (ignored)

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def print_stats(self):
        print("\n  ── Receiver Statistics ───────────────────────────")
        for k, v in self.stats.items():
            print(f"     {k:20s}: {v}")
        print("  ────────────────────────────────────────────────")
