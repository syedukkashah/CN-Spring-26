"""
main.py - Test Runner for RDT 3.0, GBN, and Selective Repeat
=============================================================

Usage
-----
    python main.py                    # run all protocols with all scenarios
    python main.py --protocol rdt30   # only rdt 3.0
    python main.py --protocol gbn     # only Go-Back-N
    python main.py --protocol sr      # only Selective Repeat
    python main.py --scenario loss    # only the packet-loss scenario

Configurable parameters (edit DEFAULTS below or pass --help):
    --packet-size     bytes per payload chunk  (default 8)
    --num-packets     number of packets        (default 6)
    --window-size     GBN / SR window          (default 3)
    --timeout         retransmit timeout (s)   (default 1.0)
    --loss-prob       channel loss probability (default 0.3)
    --corrupt-prob    corruption probability   (default 0.2)
    --delay-min/max   delay range in seconds   (default 0/0.2)
    --seed            random seed              (default 42)
    --verbose         show detailed FSM logs
"""

import argparse
import time
import threading
from network import NetworkChannel
from rdt30 import RDT30Sender, RDT30Receiver
from gbn   import GBNSender,   GBNReceiver
from sr    import SRSender,    SRReceiver


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

SEPARATOR = "=" * 64


def make_data(num_packets: int, packet_size: int) -> bytes:
    """Create a predictable byte string of exactly num_packets * packet_size bytes."""
    pattern = bytes(range(256))
    total   = num_packets * packet_size
    return (pattern * (total // 256 + 1))[:total]


def verify(original: bytes, received: bytes, label: str) -> bool:
    ok = original == received
    status = "✓ PASS" if ok else "✗ FAIL"
    print(f"\n  [{label}] Data integrity check: {status}")
    if not ok:
        print(f"   Sent    : {original[:32]!r}{'...' if len(original)>32 else ''}")
        print(f"   Received: {received[:32]!r}{'...' if len(received)>32 else ''}")
    return ok


# ──────────────────────────────────────────────────────────────────────────────
# Per-protocol runner functions
# ──────────────────────────────────────────────────────────────────────────────

def run_rdt30(data: bytes, loss: float, corrupt: float,
              delay: tuple, timeout: float, seed: int,
              verbose: bool, label: str):
    """Run one rdt 3.0 scenario."""
    print(f"\n{SEPARATOR}")
    print(f"  RDT 3.0  |  {label}")
    print(f"  loss={loss:.0%}  corrupt={corrupt:.0%}  "
          f"delay={delay}  timeout={timeout}s")
    print(SEPARATOR)

    # Two separate channels (one each direction)
    fwd_ch  = NetworkChannel(loss, corrupt, delay, seed=seed,   verbose=verbose)
    back_ch = NetworkChannel(loss, corrupt, delay, seed=seed+1, verbose=verbose)

    # Receiver must be created first so we can pass on_ack reference
    receiver = RDT30Receiver(ack_channel=back_ch, verbose=verbose)
    sender   = RDT30Sender(channel=fwd_ch, ack_channel=back_ch,
                           timeout=timeout, packet_size=len(data)//max(1,len(data)//8),
                           verbose=verbose)

    # Wire the forward channel to deliver to receiver.receive
    # (done by passing receiver.receive as the deliver_fn when transmitting)
    # We monkey-patch: wrap channel.transmit so ACK goes back to sender._on_ack_received
    original_fwd_transmit = fwd_ch.transmit

    def wired_fwd_transmit(pkt, _ignored_fn):
        original_fwd_transmit(pkt, receiver.receive)

    fwd_ch.transmit = wired_fwd_transmit

    original_back_transmit = back_ch.transmit

    def wired_back_transmit(pkt, _ignored_fn):
        original_back_transmit(pkt, sender._on_ack_received)

    back_ch.transmit = wired_back_transmit

    sender.send(data)
    time.sleep(timeout + 0.5)   # allow any in-flight retransmits to settle

    verify(data, bytes(receiver.received_data), "RDT 3.0")
    fwd_ch.print_stats()
    receiver.print_stats()


def run_gbn(data: bytes, loss: float, corrupt: float,
            delay: tuple, timeout: float, window: int,
            seed: int, verbose: bool, label: str,
            packet_size: int = 8):
    """Run one GBN scenario."""
    print(f"\n{SEPARATOR}")
    print(f"  Go-Back-N (N={window})  |  {label}")
    print(f"  loss={loss:.0%}  corrupt={corrupt:.0%}  "
          f"delay={delay}  timeout={timeout}s")
    print(SEPARATOR)

    seq_space = 2 * window + 2

    fwd_ch  = NetworkChannel(loss, corrupt, delay, seed=seed,   verbose=verbose)
    back_ch = NetworkChannel(loss, corrupt, delay, seed=seed+1, verbose=verbose)

    receiver = GBNReceiver(ack_channel=back_ch, seq_space=seq_space,
                           verbose=verbose)
    sender   = GBNSender(channel=fwd_ch, ack_channel=back_ch,
                         window_size=window, timeout=timeout,
                         packet_size=packet_size, verbose=verbose)

    # Wire channels
    orig_fwd  = fwd_ch.transmit
    orig_back = back_ch.transmit

    def wired_fwd(pkt, _):
        orig_fwd(pkt, receiver.receive)

    def wired_back(pkt, _):
        orig_back(pkt, sender.on_ack)

    fwd_ch.transmit  = wired_fwd
    back_ch.transmit = wired_back

    sender.send(data)

    verify(data, bytes(receiver.received_data), "GBN")
    fwd_ch.print_stats()
    receiver.print_stats()


def run_sr(data: bytes, loss: float, corrupt: float,
           delay: tuple, timeout: float, window: int,
           seed: int, verbose: bool, label: str,
           packet_size: int = 8):
    """Run one SR scenario."""
    print(f"\n{SEPARATOR}")
    print(f"  Selective Repeat (N={window})  |  {label}")
    print(f"  loss={loss:.0%}  corrupt={corrupt:.0%}  "
          f"delay={delay}  timeout={timeout}s")
    print(SEPARATOR)

    seq_space = 2 * window

    fwd_ch  = NetworkChannel(loss, corrupt, delay, seed=seed,   verbose=verbose)
    back_ch = NetworkChannel(loss, corrupt, delay, seed=seed+1, verbose=verbose)

    receiver = SRReceiver(ack_channel=back_ch, window_size=window,
                          seq_space=seq_space, verbose=verbose)
    sender   = SRSender(channel=fwd_ch, ack_channel=back_ch,
                        window_size=window, timeout=timeout,
                        packet_size=packet_size, verbose=verbose)

    orig_fwd  = fwd_ch.transmit
    orig_back = back_ch.transmit

    def wired_fwd(pkt, _):
        orig_fwd(pkt, receiver.receive)

    def wired_back(pkt, _):
        orig_back(pkt, sender.on_ack)

    fwd_ch.transmit  = wired_fwd
    back_ch.transmit = wired_back

    sender.send(data)

    verify(data, bytes(receiver.received_data), "SR")
    fwd_ch.print_stats()
    receiver.print_stats()


# ──────────────────────────────────────────────────────────────────────────────
# Scenario definitions
# ──────────────────────────────────────────────────────────────────────────────

SCENARIOS = {
    "clean": {
        "label":   "No Loss / No Corruption / No Delay",
        "loss":    0.0,
        "corrupt": 0.0,
        "delay":   (0.0, 0.0),
    },
    "loss": {
        "label":   "Packet Loss (30%)",
        "loss":    0.3,
        "corrupt": 0.0,
        "delay":   (0.0, 0.0),
    },
    "corrupt": {
        "label":   "Packet Corruption (30%)",
        "loss":    0.0,
        "corrupt": 0.3,
        "delay":   (0.0, 0.0),
    },
    "delay": {
        "label":   "Delayed Packets (0–0.4s)",
        "loss":    0.0,
        "corrupt": 0.0,
        "delay":   (0.05, 0.4),
    },
    "combined": {
        "label":   "Combined: Loss 20% + Corrupt 15% + Delay 0–0.3s",
        "loss":    0.2,
        "corrupt": 0.15,
        "delay":   (0.0, 0.3),
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RDT Protocol Simulator")
    parser.add_argument("--protocol",    choices=["rdt30", "gbn", "sr", "all"],
                        default="all")
    parser.add_argument("--scenario",
                        choices=list(SCENARIOS.keys()) + ["all"],
                        default="all")
    parser.add_argument("--packet-size", type=int,   default=8)
    parser.add_argument("--num-packets", type=int,   default=6)
    parser.add_argument("--window-size", type=int,   default=3)
    parser.add_argument("--timeout",     type=float, default=1.0)
    parser.add_argument("--seed",        type=int,   default=42)
    parser.add_argument("--verbose",     action="store_true", default=False)
    args = parser.parse_args()

    data = make_data(args.num_packets, args.packet_size)

    protocols = (["rdt30", "gbn", "sr"]
                 if args.protocol == "all" else [args.protocol])
    scenarios = (list(SCENARIOS.keys())
                 if args.scenario == "all" else [args.scenario])

    print(f"\n{'#'*64}")
    print(f"  RDT Protocol Simulator")
    print(f"  Payload: {len(data)} bytes ({args.num_packets} × {args.packet_size}B packets)")
    print(f"  Protocols: {protocols}")
    print(f"  Scenarios: {scenarios}")
    print(f"{'#'*64}")

    for proto in protocols:
        for scen_key in scenarios:
            s = SCENARIOS[scen_key]
            kwargs = dict(
                data    = data,
                loss    = s["loss"],
                corrupt = s["corrupt"],
                delay   = s["delay"],
                timeout = args.timeout,
                seed    = args.seed,
                verbose = args.verbose,
                label   = s["label"],
            )

            if proto == "rdt30":
                run_rdt30(**kwargs)

            elif proto == "gbn":
                run_gbn(**kwargs,
                        window=args.window_size,
                        packet_size=args.packet_size)

            elif proto == "sr":
                run_sr(**kwargs,
                       window=args.window_size,
                       packet_size=args.packet_size)

    print(f"\n{'#'*64}")
    print("  All tests complete.")
    print(f"{'#'*64}\n")


if __name__ == "__main__":
    main()
