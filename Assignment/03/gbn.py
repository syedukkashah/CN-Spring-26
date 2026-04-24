"""
gbn.py - Go-Back-N (GBN) Protocol
===================================

Protocol behaviour
------------------
Sender
  • Maintains a window of size N (max unacknowledged packets).
  • Can send multiple packets without waiting for individual ACKs.
  • Uses CUMULATIVE ACKs: ACK(n) means all packets up to n are received.
  • One shared timer for the oldest unacknowledged packet.
  • On timeout → retransmit ALL packets in the window (base … nextseqnum-1).
  • Window advances when base's ACK is received.

Receiver
  • Accepts ONLY in-order packets.
  • Discards out-of-order packets (but re-ACKs the last correct one).
  • Sends ACK for the highest in-order seq received.

Sequence number space
  • Must be ≥ window_size + 1.  We use 2 * window_size to be safe.

FSM states
----------
Sender  : state is implicitly encoded in (base, nextseqnum, window_size).
          Conceptually: READY (can send) ↔ WINDOW_FULL (must wait for ACK).
Receiver: single state WAIT_FOR_PKT — always waiting; behavior changes on
          whether seq_num == expected_seq.
"""

import threading
import time
from typing import List, Optional
from network import Packet, NetworkChannel


# ──────────────────────────────────────────────────────────────────────────────
class GBNSender:
    """
    Go-Back-N sender.

    Parameters
    ----------
    channel      : NetworkChannel  – forward channel
    ack_channel  : NetworkChannel  – backward channel (ACKs from receiver)
    window_size  : int             – maximum number of unACKed packets in flight
    timeout      : float           – retransmit timeout in seconds
    packet_size  : int             – bytes per packet payload
    verbose      : bool
    """

    def __init__(self, channel: NetworkChannel, ack_channel: NetworkChannel,
                 window_size: int = 4, timeout: float = 1.0,
                 packet_size: int = 8, verbose: bool = True):

        self.channel     = channel
        self.ack_channel = ack_channel
        self.window_size = window_size
        self.timeout     = timeout
        self.packet_size = packet_size
        self.verbose     = verbose

        # Sequence number space (must be > window_size)
        self.seq_space = 2 * window_size + 2

        # Window state
        self.base        = 0          # oldest unACKed seq
        self.nextseq     = 0          # next seq to send
        self.packets: List[Optional[Packet]] = []   # buffered packets

        # Synchronisation
        self._lock       = threading.Lock()
        self._window_sem = threading.Semaphore(window_size)  # slots available
        self._done_event = threading.Event()
        self._timer: Optional[threading.Timer] = None

        # Stats
        self.stats = {"packets_sent": 0, "retransmissions": 0,
                      "acks_received": 0, "go_backs": 0, "total_time": 0.0}

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def send(self, data: bytes) -> None:
        """Reliably transmit *data*."""
        start = time.time()
        chunks = [data[i:i + self.packet_size]
                  for i in range(0, len(data), self.packet_size)]
        total  = len(chunks)

        self._log(f"\n[GBN SENDER] Starting: {total} packet(s), "
                  f"window={self.window_size}, seq_space={self.seq_space}")

        # Build packet list (seq numbers wrap around seq_space)
        self.packets = [
            Packet(seq_num=i % self.seq_space, data=chunks[i])
            for i in range(total)
        ]

        # Launch sender thread that fills the pipeline
        self._total     = total
        self._pkt_index = 0           # index into self.packets to send next

        sender_thread = threading.Thread(target=self._send_loop, daemon=True)
        sender_thread.start()
        self._done_event.wait()       # block until all ACKed

        self.stats["total_time"] = time.time() - start
        self._log(f"\n[GBN SENDER] All {total} packet(s) delivered "
                  f"in {self.stats['total_time']:.3f}s")
        self._print_stats()

    # ──────────────────────────────────────────────────────────────────
    # Sender pipeline loop
    # ──────────────────────────────────────────────────────────────────

    def _send_loop(self):
        while self._pkt_index < self._total:
            self._window_sem.acquire()      # block if window is full

            with self._lock:
                if self._pkt_index >= self._total:
                    break
                pkt = self.packets[self._pkt_index]
                self._log(f"\n[GBN SENDER] Sending pkt {self._pkt_index+1}/"
                          f"{self._total} (seq={pkt.seq_num}), "
                          f"window=[{self.base % self.seq_space}.."
                          f"{(self.nextseq) % self.seq_space}]")

                self.stats["packets_sent"] += 1
                self.nextseq     += 1
                self._pkt_index  += 1

                # Start / restart timer when base packet is sent
                if self.base == self.nextseq - 1:
                    self._start_timer()

            self.channel.transmit(pkt, self.on_ack)

    # ──────────────────────────────────────────────────────────────────
    # ACK handler (called by channel)
    # ──────────────────────────────────────────────────────────────────

    def on_ack(self, ack_pkt: Packet) -> None:
        with self._lock:
            self.stats["acks_received"] += 1

            if ack_pkt.is_corrupt():
                self._log(f"[GBN SENDER] Corrupt ACK ignored")
                return

            ack_num   = ack_pkt.ack_num
            # Determine how many packets are acknowledged
            # (handle wrap-around in sequence space)
            base_seq  = self.packets[self.base].seq_num if self.base < self._total else -1

            # Find the absolute packet index this ACK corresponds to
            acked_idx = self._seq_to_idx(ack_num)
            if acked_idx is None:
                self._log(f"[GBN SENDER] ACK(ack_num={ack_num}) out of window — ignored")
                return

            self._log(f"[GBN SENDER] ACK(ack_num={ack_num}) → "
                      f"advancing base from {self.base} to {acked_idx + 1}")

            # Release window slots for all newly acknowledged packets
            newly_acked = acked_idx - self.base + 1
            for _ in range(newly_acked):
                self._window_sem.release()

            self.base = acked_idx + 1

            if self.base == self.nextseq:
                self._stop_timer()
            else:
                self._restart_timer()

            # Signal completion when all packets are ACKed
            if self.base >= self._total:
                self._done_event.set()

    # ──────────────────────────────────────────────────────────────────
    # Timer management
    # ──────────────────────────────────────────────────────────────────

    def _start_timer(self):
        self._stop_timer()
        self._timer = threading.Timer(self.timeout, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

    def _stop_timer(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _restart_timer(self):
        self._start_timer()

    def _on_timeout(self):
        with self._lock:
            self.stats["go_backs"]       += 1
            self.stats["retransmissions"] += (self.nextseq - self.base)
            self._log(f"\n[GBN SENDER] TIMEOUT — going back to seq="
                      f"{self.packets[self.base].seq_num}, "
                      f"retransmitting {self.nextseq - self.base} packet(s)")

            # Retransmit all packets in [base, nextseq)
            pkts_to_resend = list(range(self.base, self.nextseq))
            self._restart_timer()

        for idx in pkts_to_resend:
            if idx < self._total:
                pkt = self.packets[idx]
                self._log(f"  [GBN SENDER] Retransmitting seq={pkt.seq_num}")
                self.channel.transmit(pkt, self.on_ack)

    # ──────────────────────────────────────────────────────────────────
    # Utility
    # ──────────────────────────────────────────────────────────────────

    def _seq_to_idx(self, seq_num: int) -> Optional[int]:
        """
        Convert a received ack_num to the absolute packet index.
        Returns None if the seq_num is not within the current window.
        """
        for idx in range(self.base, min(self.nextseq, self._total)):
            if self.packets[idx].seq_num == seq_num:
                return idx
        return None

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def _print_stats(self):
        print("\n  ── GBN Sender Statistics ─────────────────────────")
        for k, v in self.stats.items():
            print(f"     {k:20s}: {v}")
        print("  ────────────────────────────────────────────────")


# ──────────────────────────────────────────────────────────────────────────────
class GBNReceiver:
    """
    Go-Back-N receiver.

    Accepts ONLY the next expected packet.  All out-of-order packets are
    discarded and the last valid ACK is re-sent.
    """

    def __init__(self, ack_channel: NetworkChannel, seq_space: int = 10,
                 verbose: bool = True):
        self.ack_channel  = ack_channel
        self.seq_space    = seq_space
        self.verbose      = verbose

        self.expected_seq = 0
        self.received_data = bytearray()

        self.stats = {"pkts_received": 0, "pkts_delivered": 0,
                      "pkts_discarded": 0, "acks_sent": 0, "corrupted": 0}

    # ──────────────────────────────────────────────────────────────────

    def receive(self, pkt: Packet) -> None:
        self.stats["pkts_received"] += 1
        self._log(f"\n[GBN RECEIVER] Got: {pkt} (expected seq={self.expected_seq})")

        # ── Corrupt? ──────────────────────────────────────────────────
        if pkt.is_corrupt():
            self.stats["corrupted"] += 1
            self._log(f"[GBN RECEIVER] CORRUPT — re-ACKing seq="
                      f"{(self.expected_seq - 1) % self.seq_space}")
            # Re-ACK the last correctly received packet
            self._send_ack((self.expected_seq - 1) % self.seq_space)
            return

        # ── Out of order? ─────────────────────────────────────────────
        if pkt.seq_num != self.expected_seq:
            self.stats["pkts_discarded"] += 1
            self._log(f"[GBN RECEIVER] Out-of-order (got {pkt.seq_num}, "
                      f"expected {self.expected_seq}) — DISCARDING, "
                      f"re-ACKing {(self.expected_seq - 1) % self.seq_space}")
            self._send_ack((self.expected_seq - 1) % self.seq_space)
            return

        # ── In order ─────────────────────────────────────────────────
        self._log(f"[GBN RECEIVER] Delivering seq={pkt.seq_num}: {pkt.data!r}")
        self.received_data.extend(pkt.data)
        self.stats["pkts_delivered"] += 1
        self._send_ack(self.expected_seq)
        self.expected_seq = (self.expected_seq + 1) % self.seq_space

    def _send_ack(self, ack_num: int):
        self.stats["acks_sent"] += 1
        ack = Packet(seq_num=0, is_ack=True, ack_num=ack_num)
        self.ack_channel.transmit(ack, lambda _: None)

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def print_stats(self):
        print("\n  ── GBN Receiver Statistics ───────────────────────")
        for k, v in self.stats.items():
            print(f"     {k:20s}: {v}")
        print("  ────────────────────────────────────────────────")
