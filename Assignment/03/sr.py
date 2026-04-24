"""
sr.py - Selective Repeat (SR) Protocol
========================================

Protocol behaviour
------------------
Sender
  • Window of size N packets may be in-flight simultaneously.
  • Each unACKed packet has its OWN independent timer.
  • On timeout for packet k → retransmit ONLY packet k.
  • Window slides forward only when base packet is ACKed.
  • ACKs may arrive out of order; each is handled individually.

Receiver
  • Also maintains a window of size N.
  • Accepts and BUFFERS out-of-order packets within its window.
  • Delivers a contiguous block to upper layer when the buffer fills in.
  • Sends individual ACKs for every correctly received packet.
  • For packets BELOW the window (already ACKed duplicates) → re-ACK.
  • For packets ABOVE the window → discard silently.

Key constraint
  • Window size N must be ≤ seq_space / 2 to avoid ambiguity between
    "new" and "retransmitted" packets.  We enforce seq_space = 2 * N.

FSM states
----------
Sender  : READY (within window) | WINDOW_FULL (waiting for ACKs)
          Each outstanding packet is in either UNACKED or ACKED sub-state.
Receiver: Every slot in the receiver window is EMPTY | FILLED.
          Upper-layer delivery happens when slot 0 becomes FILLED.
"""

import threading
import time
from typing import Dict, List, Optional
from network import Packet, NetworkChannel


# ──────────────────────────────────────────────────────────────────────────────
class _SenderSlot:
    """Tracks the state of one packet in the sender's window."""
    def __init__(self):
        self.packet: Optional[Packet] = None
        self.acked:  bool             = False
        self.timer:  Optional[threading.Timer] = None


# ──────────────────────────────────────────────────────────────────────────────
class SRSender:
    """
    Selective Repeat sender.

    Parameters
    ----------
    channel      : NetworkChannel
    ack_channel  : NetworkChannel
    window_size  : int    – N (window size)
    timeout      : float  – per-packet retransmit timeout
    packet_size  : int    – bytes per payload chunk
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

        # Sequence number space: must be ≥ 2 * window_size
        self.seq_space = 2 * window_size

        # Window state: circular buffer indexed by absolute packet number
        self.base    = 0   # oldest unACKed absolute packet index
        self.nextseq = 0   # next absolute packet index to send

        # Slot map: absolute_index → _SenderSlot
        self._slots: Dict[int, _SenderSlot] = {}

        # Synchronisation
        self._lock       = threading.Lock()
        self._window_sem = threading.Semaphore(window_size)
        self._done_event = threading.Event()

        # Stats
        self.stats = {"packets_sent": 0, "retransmissions": 0,
                      "acks_received": 0, "total_time": 0.0}

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def send(self, data: bytes) -> None:
        """Reliably transmit *data*."""
        start  = time.time()
        chunks = [data[i:i + self.packet_size]
                  for i in range(0, len(data), self.packet_size)]
        self._total = len(chunks)

        self._log(f"\n[SR SENDER] Starting: {self._total} packet(s), "
                  f"window={self.window_size}, seq_space={self.seq_space}")

        # Build packets
        self._packets = [
            Packet(seq_num=i % self.seq_space, data=chunks[i])
            for i in range(self._total)
        ]

        # Initialise slots
        for i in range(self._total):
            self._slots[i] = _SenderSlot()
            self._slots[i].packet = self._packets[i]

        # Launch the send loop
        t = threading.Thread(target=self._send_loop, daemon=True)
        t.start()
        self._done_event.wait()

        self.stats["total_time"] = time.time() - start
        self._log(f"\n[SR SENDER] All {self._total} packet(s) delivered "
                  f"in {self.stats['total_time']:.3f}s")
        self._print_stats()

    # ──────────────────────────────────────────────────────────────────
    # Send loop
    # ──────────────────────────────────────────────────────────────────

    def _send_loop(self):
        idx = 0
        while idx < self._total:
            self._window_sem.acquire()     # block until a window slot is free

            with self._lock:
                if idx >= self._total:
                    break
                pkt  = self._packets[idx]
                slot = self._slots[idx]

                self._log(f"\n[SR SENDER] Sending pkt {idx+1}/{self._total} "
                          f"(seq={pkt.seq_num}), "
                          f"window=[{self.base}..{self.nextseq}]")

                self.stats["packets_sent"] += 1
                self.nextseq += 1
                self._start_timer(idx)

            self.channel.transmit(pkt, self.on_ack)
            idx += 1

    # ──────────────────────────────────────────────────────────────────
    # ACK handler
    # ──────────────────────────────────────────────────────────────────

    def on_ack(self, ack_pkt: Packet) -> None:
        with self._lock:
            self.stats["acks_received"] += 1

            if ack_pkt.is_corrupt():
                self._log(f"[SR SENDER] Corrupt ACK — ignored")
                return

            # Find which absolute index this ACK corresponds to
            acked_idx = self._seq_to_idx(ack_pkt.ack_num)
            if acked_idx is None:
                self._log(f"[SR SENDER] ACK(seq={ack_pkt.ack_num}) not in window — ignored")
                return

            slot = self._slots[acked_idx]
            if slot.acked:
                self._log(f"[SR SENDER] Duplicate ACK(seq={ack_pkt.ack_num}) — ignored")
                return

            # Mark this slot as ACKed and cancel its timer
            self._log(f"[SR SENDER] ACK(ack_num={ack_pkt.ack_num}) → "
                      f"marking pkt {acked_idx+1} as ACKed")
            slot.acked = True
            self._cancel_timer(acked_idx)

            # Slide the window forward over consecutive ACKed slots
            while (self.base < self._total and
                   self._slots[self.base].acked):
                self._log(f"  [SR SENDER] Window base advances past pkt {self.base+1}")
                del self._slots[self.base]
                self.base += 1
                self._window_sem.release()   # one more slot available

            if self.base >= self._total:
                self._done_event.set()

    # ──────────────────────────────────────────────────────────────────
    # Per-packet timers
    # ──────────────────────────────────────────────────────────────────

    def _start_timer(self, idx: int):
        self._cancel_timer(idx)
        timer = threading.Timer(self.timeout, self._on_timeout, args=(idx,))
        timer.daemon = True
        timer.start()
        self._slots[idx].timer = timer

    def _cancel_timer(self, idx: int):
        slot = self._slots.get(idx)
        if slot and slot.timer:
            slot.timer.cancel()
            slot.timer = None

    def _on_timeout(self, idx: int):
        with self._lock:
            slot = self._slots.get(idx)
            if slot is None or slot.acked:
                return   # packet already ACKed — nothing to do

            pkt = self._packets[idx]
            self.stats["retransmissions"] += 1
            self._log(f"\n[SR SENDER] TIMEOUT for pkt {idx+1} "
                      f"(seq={pkt.seq_num}) — retransmitting only this packet")
            self._start_timer(idx)         # restart its timer

        self.channel.transmit(pkt, self.on_ack)

    # ──────────────────────────────────────────────────────────────────
    # Utility
    # ──────────────────────────────────────────────────────────────────

    def _seq_to_idx(self, seq_num: int) -> Optional[int]:
        """Map a seq_num back to an absolute packet index within the window."""
        for idx in range(self.base, min(self.nextseq, self._total)):
            if self._packets[idx].seq_num == seq_num:
                return idx
        return None

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def _print_stats(self):
        print("\n  ── SR Sender Statistics ──────────────────────────")
        for k, v in self.stats.items():
            print(f"     {k:20s}: {v}")
        print("  ────────────────────────────────────────────────")


# ──────────────────────────────────────────────────────────────────────────────
class SRReceiver:
    """
    Selective Repeat receiver.

    Maintains a receive window.  Out-of-order packets within the window
    are buffered.  In-order delivery to the upper layer happens whenever
    the leading edge of the buffer fills.
    """

    def __init__(self, ack_channel: NetworkChannel,
                 window_size: int = 4, seq_space: int = 8,
                 verbose: bool = True):

        self.ack_channel = ack_channel
        self.window_size = window_size
        self.seq_space   = seq_space
        self.verbose     = verbose

        # rcv_base is the absolute index of the lowest expected packet
        self.rcv_base    = 0

        # Buffer: seq_num → Packet (for out-of-order storage)
        self._buffer: Dict[int, Packet] = {}

        self.received_data = bytearray()

        self.stats = {"pkts_received": 0, "pkts_buffered": 0,
                      "pkts_delivered": 0, "acks_sent": 0,
                      "corrupted": 0, "out_of_window": 0}

    # ──────────────────────────────────────────────────────────────────

    def receive(self, pkt: Packet) -> None:
        self.stats["pkts_received"] += 1
        base_seq = self.rcv_base % self.seq_space
        self._log(f"\n[SR RECEIVER] Got: {pkt} "
                  f"(rcv_base_seq={base_seq}, "
                  f"window=[{base_seq}..{(base_seq + self.window_size - 1) % self.seq_space}])")

        # ── Corrupt ──────────────────────────────────────────────────
        if pkt.is_corrupt():
            self.stats["corrupted"] += 1
            self._log(f"[SR RECEIVER] CORRUPT — discarding (no ACK sent)")
            return   # SR receiver sends NO ACK for corrupt packets

        seq = pkt.seq_num

        # ── Classify sequence number ──────────────────────────────────
        # Window:   [rcv_base_seq, rcv_base_seq + window_size - 1]  (mod seq_space)
        # Below window: already received & ACKed duplicates → re-ACK
        # Above window: too far ahead → discard

        if self._in_window(seq, base_seq):
            # ── In receive window: buffer it ─────────────────────────
            if seq not in self._buffer:
                self._log(f"[SR RECEIVER] Buffering seq={seq}")
                self._buffer[seq] = pkt
                self.stats["pkts_buffered"] += 1
            else:
                self._log(f"[SR RECEIVER] Duplicate in window seq={seq} — re-ACKing")

            # ACK this packet individually
            self._send_ack(seq)

            # Deliver consecutive in-order packets from base
            self._try_deliver(base_seq)

        elif self._in_prev_window(seq, base_seq):
            # ── Below window: already delivered, re-ACK ──────────────
            self._log(f"[SR RECEIVER] Below window (duplicate) seq={seq} — re-ACKing")
            self._send_ack(seq)

        else:
            # ── Out of window entirely: discard ───────────────────────
            self.stats["out_of_window"] += 1
            self._log(f"[SR RECEIVER] seq={seq} outside both windows — discarding")

    # ──────────────────────────────────────────────────────────────────
    # Delivery logic
    # ──────────────────────────────────────────────────────────────────

    def _try_deliver(self, base_seq: int):
        """Deliver as many in-order packets as possible from the buffer."""
        seq = base_seq
        while seq in self._buffer:
            pkt = self._buffer.pop(seq)
            self._log(f"[SR RECEIVER] Delivering seq={seq}: {pkt.data!r}")
            self.received_data.extend(pkt.data)
            self.stats["pkts_delivered"] += 1
            self.rcv_base += 1
            seq = self.rcv_base % self.seq_space

    # ──────────────────────────────────────────────────────────────────
    # Window membership helpers
    # ──────────────────────────────────────────────────────────────────

    def _in_window(self, seq: int, base: int) -> bool:
        """True if seq is within [base, base+window_size-1] (mod seq_space)."""
        end = (base + self.window_size - 1) % self.seq_space
        if base <= end:
            return base <= seq <= end
        else:   # wrap-around
            return seq >= base or seq <= end

    def _in_prev_window(self, seq: int, base: int) -> bool:
        """True if seq is in the previous window (already ACKed)."""
        prev_base = (base - self.window_size) % self.seq_space
        prev_end  = (base - 1) % self.seq_space
        if prev_base <= prev_end:
            return prev_base <= seq <= prev_end
        else:
            return seq >= prev_base or seq <= prev_end

    # ──────────────────────────────────────────────────────────────────

    def _send_ack(self, ack_num: int):
        self.stats["acks_sent"] += 1
        ack = Packet(seq_num=0, is_ack=True, ack_num=ack_num)
        self.ack_channel.transmit(ack, lambda _: None)

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def print_stats(self):
        print("\n  ── SR Receiver Statistics ────────────────────────")
        for k, v in self.stats.items():
            print(f"     {k:20s}: {v}")
        print("  ────────────────────────────────────────────────")
