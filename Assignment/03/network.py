"""
network.py - Unreliable Network Channel Simulator
==================================================
Simulates an unreliable network that can introduce:
  - Packet loss      (packet is dropped entirely)
  - Packet corruption (one or more bits are flipped)
  - Packet delay     (packet is held and delivered later)

All probabilities are configurable at runtime.
"""

import random
import time
import threading
from collections import deque
from typing import Optional, Callable


class Packet:
    """
    Represents a network packet.

    Fields
    ------
    seq_num   : int   - sequence number (used to detect duplicates / ordering)
    data      : bytes - payload bytes
    checksum  : int   - simple XOR checksum of seq_num + data bytes
    is_ack    : bool  - True when this packet carries an acknowledgment
    ack_num   : int   - the sequence number being acknowledged (only for ACK packets)
    """

    def __init__(self, seq_num: int, data: bytes = b"",
                 is_ack: bool = False, ack_num: int = 0):
        self.seq_num = seq_num
        self.data = data
        self.is_ack = is_ack
        self.ack_num = ack_num
        self.checksum = self._compute_checksum()

    # ------------------------------------------------------------------
    # Checksum helpers
    # ------------------------------------------------------------------

    def _compute_checksum(self) -> int:
        """XOR-based checksum over seq_num, ack_num, and every byte of data."""
        chk = self.seq_num ^ self.ack_num
        for byte in self.data:
            chk ^= byte
        return chk

    def is_corrupt(self) -> bool:
        """Return True if the stored checksum no longer matches the data."""
        return self.checksum != self._compute_checksum()

    def corrupt(self):
        """Deliberately corrupt the packet by flipping the checksum."""
        self.checksum ^= 0xFF   # flip all 8 bits of the low byte

    # ------------------------------------------------------------------
    # Pretty printing
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        if self.is_ack:
            return f"Packet(ACK={self.ack_num}, chk={self.checksum})"
        return (f"Packet(seq={self.seq_num}, "
                f"data={self.data[:12]!r}{'...' if len(self.data) > 12 else ''}, "
                f"chk={self.checksum})")


class NetworkChannel:
    """
    Simulated unreliable channel between sender and receiver.

    Parameters
    ----------
    loss_prob    : float  - probability [0, 1) that a packet is dropped
    corrupt_prob : float  - probability [0, 1) that a packet is corrupted
    delay_range  : tuple  - (min_delay_s, max_delay_s) for artificial delay
    seed         : int    - random seed for reproducibility (None = random)
    verbose      : bool   - print every channel event when True
    """

    def __init__(self,
                 loss_prob: float = 0.0,
                 corrupt_prob: float = 0.0,
                 delay_range: tuple = (0.0, 0.0),
                 seed: Optional[int] = None,
                 verbose: bool = True):

        if seed is not None:
            random.seed(seed)

        self.loss_prob = loss_prob
        self.corrupt_prob = corrupt_prob
        self.delay_min, self.delay_max = delay_range
        self.verbose = verbose

        # Statistics counters
        self.stats = {
            "sent":      0,
            "lost":      0,
            "corrupted": 0,
            "delayed":   0,
            "delivered": 0,
        }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def transmit(self, packet: Packet,
                 deliver_fn: Callable[[Packet], None]) -> None:
        """
        Attempt to transmit *packet* through the unreliable channel.

        The packet is delivered (possibly after a delay) by calling
        deliver_fn(packet).  The call is non-blocking — delayed packets
        are scheduled on a background thread.
        """
        self.stats["sent"] += 1
        direction = "ACK" if packet.is_ack else "DATA"

        # ---- 1. Packet loss ----------------------------------------
        if random.random() < self.loss_prob:
            self.stats["lost"] += 1
            self._log(f"  [CHANNEL] {direction} pkt LOST   → {packet}")
            return          # packet is dropped — deliver_fn is never called

        # ---- 2. Corruption -----------------------------------------
        if random.random() < self.corrupt_prob:
            self.stats["corrupted"] += 1
            packet.corrupt()
            self._log(f"  [CHANNEL] {direction} pkt CORRUPT → {packet}")

        # ---- 3. Delay ----------------------------------------------
        delay = random.uniform(self.delay_min, self.delay_max)
        if delay > 0:
            self.stats["delayed"] += 1
            self._log(f"  [CHANNEL] {direction} pkt DELAYED {delay:.3f}s → {packet}")
            t = threading.Timer(delay, self._deliver, args=(packet, deliver_fn))
            t.daemon = True
            t.start()
        else:
            self._deliver(packet, deliver_fn)

    def _deliver(self, packet: Packet,
                 deliver_fn: Callable[[Packet], None]) -> None:
        self.stats["delivered"] += 1
        self._log(f"  [CHANNEL] Delivered → {packet}")
        deliver_fn(packet)

    def reset_stats(self):
        for key in self.stats:
            self.stats[key] = 0

    def print_stats(self):
        print("\n  ── Channel Statistics ──────────────────────────")
        for k, v in self.stats.items():
            print(f"     {k:12s}: {v}")
        print("  ────────────────────────────────────────────────")

    def _log(self, msg: str):
        if self.verbose:
            print(msg)
