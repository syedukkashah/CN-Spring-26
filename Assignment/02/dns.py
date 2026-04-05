"""
DNS Implementation - CS3001 Computer Networks Assignment II
============================================================
Simulates Root, TLD, and Authoritative DNS servers with:
- Recursive (sequential) query resolution
- Iterative query resolution
- DNS message format with 16-bit ID and flags
- DNS record types: A, NS, MX, CNAME
- Local DNS caching with LRU eviction (auto-flush when full)
- Cache hit demonstration for repeated queries
"""

import random
import struct
import time
from collections import OrderedDict


# ─────────────────────────────────────────────────────────────
# SECTION 1: DNS MESSAGE FORMAT
# Each DNS message has a header with two 16-bit fields:
#   - Transaction ID (identifies query/reply pair)
#   - Flags (QR bit, opcode, recursion desired, etc.)
# ─────────────────────────────────────────────────────────────

class DNSFlags:
    """
    Encodes the 16-bit flags field of a DNS message header.
    Bit layout (from MSB):
      QR(1) | Opcode(4) | AA(1) | TC(1) | RD(1) | RA(1) | Z(3) | RCODE(4)
    """
    def __init__(self, qr=0, opcode=0, aa=0, tc=0, rd=1, ra=0, rcode=0):
        self.qr     = qr      # 0 = query, 1 = response
        self.opcode = opcode  # 0 = standard query
        self.aa     = aa      # 1 = authoritative answer
        self.tc     = tc      # 1 = message truncated
        self.rd     = rd      # 1 = recursion desired
        self.ra     = ra      # 1 = recursion available
        self.rcode  = rcode   # 0 = no error

    def encode(self) -> int:
        """Pack all flag bits into a single 16-bit integer."""
        value = 0
        value |= (self.qr     & 0x1) << 15
        value |= (self.opcode & 0xF) << 11
        value |= (self.aa     & 0x1) << 10
        value |= (self.tc     & 0x1) << 9
        value |= (self.rd     & 0x1) << 8
        value |= (self.ra     & 0x1) << 7
        value |= (self.rcode  & 0xF)
        return value

    @classmethod
    def decode(cls, value: int) -> 'DNSFlags':
        """Unpack a 16-bit integer back into individual flag fields."""
        f = cls()
        f.qr     = (value >> 15) & 0x1
        f.opcode = (value >> 11) & 0xF
        f.aa     = (value >> 10) & 0x1
        f.tc     = (value >> 9)  & 0x1
        f.rd     = (value >> 8)  & 0x1
        f.ra     = (value >> 7)  & 0x1
        f.rcode  = value         & 0xF
        return f

    def __str__(self):
        labels = []
        if self.qr:   labels.append("QR=Response")
        else:         labels.append("QR=Query")
        if self.rd:   labels.append("RD")
        if self.ra:   labels.append("RA")
        if self.aa:   labels.append("AA")
        return f"[{' | '.join(labels)} | RCODE={self.rcode}]"


class DNSMessage:
    """
    Represents a DNS protocol message (Figure 02 from the assignment).
    Header: ID(16) + Flags(16) + counts for questions/answers/authority/additional
    Body:   Questions, Answer RRs, Authority RRs, Additional RRs
    """
    def __init__(self, msg_id: int, flags: DNSFlags,
                 questions=None, answers=None, authority=None, additional=None):
        self.msg_id     = msg_id & 0xFFFF   # 16-bit transaction ID
        self.flags      = flags
        self.questions  = questions  or []   # list of (name, qtype)
        self.answers    = answers    or []   # list of ResourceRecord
        self.authority  = authority  or []   # list of ResourceRecord (NS records)
        self.additional = additional or []   # list of ResourceRecord (glue A records)

    def encode_header(self) -> bytes:
        """
        Serialize the 12-byte fixed DNS header using struct packing.
        Format: !HHHHHH = 6 unsigned shorts in network byte order.
        """
        return struct.pack("!HHHHHH",
            self.msg_id,
            self.flags.encode(),
            len(self.questions),
            len(self.answers),
            len(self.authority),
            len(self.additional)
        )

    def __str__(self):
        lines = [
            f"  MSG ID   : 0x{self.msg_id:04X}  ({self.msg_id})",
            f"  Flags    : {self.flags}",
            f"  Questions: {len(self.questions)}",
            f"  Answers  : {len(self.answers)}",
            f"  Authority: {len(self.authority)}",
            f"  Additional:{len(self.additional)}",
        ]
        for q in self.questions:
            lines.append(f"    ? {q[0]}  TYPE={q[1]}")
        for rr in self.answers:
            lines.append(f"    A {rr}")
        for rr in self.authority:
            lines.append(f"    NS {rr}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# SECTION 2: DNS RESOURCE RECORDS
# Each record is a (name, type, value, TTL) tuple.
# Types used: A (IPv4), NS (nameserver), MX (mail), CNAME (alias)
# ─────────────────────────────────────────────────────────────

class ResourceRecord:
    """A single DNS Resource Record (RR)."""
    def __init__(self, name: str, rtype: str, value: str, ttl: int = 300):
        self.name  = name    # domain name this record belongs to
        self.rtype = rtype   # record type: A, NS, MX, CNAME, ...
        self.value = value   # record data (IP, hostname, priority+host, ...)
        self.ttl   = ttl     # seconds to live (used by caching)

    def __str__(self):
        return f"{self.name:<30} {self.rtype:<6} {self.value}  (TTL={self.ttl}s)"


# ─────────────────────────────────────────────────────────────
# SECTION 3: LRU CACHE with AUTO-FLUSH
# Simulates the local DNS server cache.
# Uses OrderedDict to track insertion order for LRU eviction.
# When capacity is full, the LEAST recently used entry is removed.
# ─────────────────────────────────────────────────────────────

class LRUCache:
    """
    Least-Recently-Used cache with a fixed capacity.
    When full, evicts the oldest (least recently used) entry.
    This simulates the local DNS resolver's cache.
    """
    def __init__(self, capacity: int = 5):
        self.capacity = capacity
        self._store   = OrderedDict()   # key -> (records, timestamp)
        self.hits     = 0
        self.misses   = 0

    def get(self, key: str):
        """Return cached records if present and not expired; else None."""
        if key not in self._store:
            self.misses += 1
            return None
        records, ts, ttl = self._store[key]
        if time.time() - ts > ttl:
            # TTL expired — remove and treat as miss
            del self._store[key]
            self.misses += 1
            return None
        # Move to end to mark as recently used
        self._store.move_to_end(key)
        self.hits += 1
        return records

    def put(self, key: str, records: list, ttl: int = 300):
        """Insert or update an entry, evicting LRU item if at capacity."""
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (records, time.time(), ttl)
        if len(self._store) > self.capacity:
            evicted_key, _ = self._store.popitem(last=False)  # pop oldest
            print(f"  [CACHE] ⚡ Auto-flush: evicted '{evicted_key}' (cache full)")

    def __contains__(self, key):
        return key in self._store

    def show(self):
        print(f"\n  [CACHE STATUS] {len(self._store)}/{self.capacity} entries  "
              f"| Hits: {self.hits}  Misses: {self.misses}")
        for i, (k, (recs, ts, ttl)) in enumerate(self._store.items()):
            age = int(time.time() - ts)
            print(f"    [{i+1}] '{k}'  age={age}s  TTL={ttl}s  ({len(recs)} record(s))")


# ─────────────────────────────────────────────────────────────
# SECTION 4: DNS SERVER ZONES (the actual data)
# In the real DNS, each server stores "zone files" with RRs.
# We model Root, TLD (.com, .edu, .org), and Authoritative servers.
# ─────────────────────────────────────────────────────────────

# ---------- Authoritative servers: store actual A, MX, NS records ----------

AUTHORITATIVE_ZONES = {
    # google.com zone (dns.google.com is the authoritative server)
    "google.com": {
        "A" : ["64.233.187.99", "72.14.207.99", "64.233.167.99"],
        "NS": ["ns1.google.com.", "ns2.google.com.", "ns3.google.com.", "ns4.google.com."],
        "MX": ["10 smtp1.google.com.", "10 smtp2.google.com.",
               "10 smtp3.google.com.", "10 smtp4.google.com."],
    },
    # youtube.com zone
    "youtube.com": {
        "A" : ["142.250.80.46", "142.250.80.78"],
        "NS": ["ns1.google.com.", "ns2.google.com."],
        "MX": ["10 aspmx.l.google.com.", "20 alt1.aspmx.l.google.com."],
    },
    # github.com zone
    "github.com": {
        "A" : ["140.82.121.4", "140.82.121.3"],
        "NS": ["ns-1707.awsdns-21.co.uk.", "ns-421.awsdns-52.com."],
        "MX": ["1 aspmx.l.google.com.", "5 alt1.aspmx.l.google.com."],
    },
    # fast.edu.pk zone (FAST-NUCES)
    "nu.edu.pk": {
        "A" : ["203.81.64.131"],
        "NS": ["ns1.nu.edu.pk.", "ns2.nu.edu.pk."],
        "MX": ["10 mail.nu.edu.pk."],
    },
    # example.org zone
    "example.org": {
        "A" : ["93.184.216.34"],
        "NS": ["a.iana-servers.net.", "b.iana-servers.net."],
        "MX": [],
    },
    # cs.umass.edu zone (from Fig 01)
    "cs.umass.edu": {
        "A" : ["128.119.240.19"],
        "NS": ["dns.cs.umass.edu."],
        "MX": ["10 mail.cs.umass.edu."],
        "CNAME": {"gaia.cs.umass.edu": "gaia.cs.umass.edu."},
    },
}

# Map: authoritative server hostname -> which zone it serves
AUTH_SERVER_MAP = {
    "dns.google.com"       : ["google.com", "youtube.com"],
    "ns1.github.com"       : ["github.com"],
    "ns1.nu.edu.pk"        : ["nu.edu.pk"],
    "a.iana-servers.net"   : ["example.org"],
    "dns.cs.umass.edu"     : ["cs.umass.edu"],
}

# ---------- TLD servers: know which authoritative server handles each 2LD ----------

TLD_ZONES = {
    ".com": {
        "google.com"   : "dns.google.com",
        "youtube.com"  : "dns.google.com",
        "github.com"   : "ns1.github.com",
    },
    ".edu": {
        "cs.umass.edu" : "dns.cs.umass.edu",
    },
    ".org": {
        "example.org"  : "a.iana-servers.net",
    },
    ".pk": {
        "nu.edu.pk"    : "ns1.nu.edu.pk",
    },
}

# ---------- Root server: knows which TLD server handles each TLD ----------

ROOT_ZONE = {
    ".com" : "tld-server-com.root-servers.net",
    ".edu" : "tld-server-edu.root-servers.net",
    ".org" : "tld-server-org.root-servers.net",
    ".pk"  : "tld-server-pk.root-servers.net",
}


# ─────────────────────────────────────────────────────────────
# SECTION 5: DNS SERVERS (Root, TLD, Authoritative)
# Each server class has a single method: query(domain)
# They return a DNSMessage with the appropriate RRs.
# ─────────────────────────────────────────────────────────────

def _new_id() -> int:
    """Generate a random 16-bit transaction ID."""
    return random.randint(0, 0xFFFF)


class RootDNSServer:
    """
    The root DNS server sits at the top of the hierarchy.
    It doesn't know final IPs — it only knows which TLD server
    is responsible for a given top-level domain (.com, .org, etc.).
    """
    name = "root-servers.net"

    def query(self, domain: str) -> DNSMessage:
        tld = self._extract_tld(domain)
        print(f"\n  [ROOT] Received query for '{domain}'  →  TLD='{tld}'")
        flags_q = DNSFlags(qr=0, rd=0)
        msg_id  = _new_id()

        if tld not in ROOT_ZONE:
            print(f"  [ROOT] ✗ Unknown TLD '{tld}'")
            flags_r = DNSFlags(qr=1, rcode=3)  # NXDOMAIN
            return DNSMessage(msg_id, flags_r, questions=[(domain, "A")])

        tld_server = ROOT_ZONE[tld]
        print(f"  [ROOT] ✓ Referring to TLD server: {tld_server}")
        flags_r = DNSFlags(qr=1, ra=0, rcode=0)
        ns_rr   = ResourceRecord(tld, "NS", tld_server, ttl=172800)
        return DNSMessage(msg_id, flags_r,
                          questions=[(domain, "A")],
                          authority=[ns_rr])

    @staticmethod
    def _extract_tld(domain: str) -> str:
        """Extract the top-level domain (e.g. 'google.com' → '.com')."""
        parts = domain.rstrip('.').split('.')
        return '.' + parts[-1]


class TLDDNSServer:
    """
    Top-Level Domain server knows which authoritative server
    is responsible for each second-level domain under its TLD.
    E.g. the .com TLD server knows google.com → dns.google.com
    """
    def __init__(self, tld: str):
        self.tld  = tld
        self.name = ROOT_ZONE.get(tld, f"tld-{tld}")
        self.zone = TLD_ZONES.get(tld, {})

    def query(self, domain: str) -> DNSMessage:
        print(f"\n  [TLD {self.tld}] Received query for '{domain}'")
        msg_id = _new_id()

        # Find the matching 2LD entry (e.g. "cs.umass.edu" for "cs.umass.edu")
        auth_server = None
        for registered_domain, server in self.zone.items():
            if domain == registered_domain or domain.endswith('.' + registered_domain):
                auth_server = server
                break

        if not auth_server:
            print(f"  [TLD {self.tld}] ✗ No entry for '{domain}'")
            flags_r = DNSFlags(qr=1, rcode=3)
            return DNSMessage(msg_id, flags_r, questions=[(domain, "A")])

        print(f"  [TLD {self.tld}] ✓ Authoritative server: {auth_server}")
        flags_r = DNSFlags(qr=1, ra=0, rcode=0)
        ns_rr   = ResourceRecord(domain, "NS", auth_server, ttl=172800)
        return DNSMessage(msg_id, flags_r,
                          questions=[(domain, "A")],
                          authority=[ns_rr])


class AuthoritativeDNSServer:
    """
    The authoritative server holds the actual DNS records for a zone.
    It answers with A (IP address), NS, and MX records.
    """
    def __init__(self, hostname: str, zones: list):
        self.hostname = hostname
        self.zones    = zones   # list of domain names this server is auth for

    def query(self, domain: str, qtype: str = "A") -> DNSMessage:
        print(f"\n  [AUTH {self.hostname}] Received query for '{domain}' type={qtype}")
        msg_id = _new_id()

        # Find the zone this domain belongs to
        zone_data = None
        for z in self.zones:
            if domain == z or domain.endswith('.' + z):
                zone_data = AUTHORITATIVE_ZONES.get(z)
                break

        if not zone_data:
            print(f"  [AUTH] ✗ Not authoritative for '{domain}'")
            flags_r = DNSFlags(qr=1, aa=1, rcode=2)  # Server failure
            return DNSMessage(msg_id, flags_r, questions=[(domain, qtype)])

        flags_r = DNSFlags(qr=1, aa=1, ra=0, rcode=0)
        answers   = []
        authority = []
        additional = []

        # Build A records
        for ip in zone_data.get("A", []):
            answers.append(ResourceRecord(domain, "A", ip, ttl=300))

        # Build NS records (go into authority section)
        for ns in zone_data.get("NS", []):
            authority.append(ResourceRecord(domain, "NS", ns, ttl=86400))

        # Build MX records (go into additional section for full info)
        for mx in zone_data.get("MX", []):
            additional.append(ResourceRecord(domain, "MX", mx, ttl=3600))

        print(f"  [AUTH] ✓ Found {len(answers)} A record(s), "
              f"{len(authority)} NS, {len(additional)} MX")

        return DNSMessage(msg_id, flags_r,
                          questions=[(domain, qtype)],
                          answers=answers,
                          authority=authority,
                          additional=additional)


# ─────────────────────────────────────────────────────────────
# SECTION 6: LOCAL DNS SERVER (RESOLVER)
# This is dns.poly.edu in Figure 01.
# It handles two resolution modes:
#   1. Recursive (sequential): local server does all the work
#   2. Iterative: local server is guided step-by-step by each server
# ─────────────────────────────────────────────────────────────

class LocalDNSServer:
    """
    The local DNS resolver (e.g. dns.poly.edu from Figure 01).
    Maintains a cache to avoid redundant queries.
    Supports both recursive and iterative resolution.
    """

    def __init__(self, name: str = "dns.poly.edu", cache_capacity: int = 5):
        self.name       = name
        self.cache      = LRUCache(capacity=cache_capacity)
        self.root       = RootDNSServer()
        # Pre-build TLD servers
        self._tld_servers = {tld: TLDDNSServer(tld) for tld in ROOT_ZONE}
        # Pre-build authoritative servers
        self._auth_servers = {}
        for hostname, zones in AUTH_SERVER_MAP.items():
            self._auth_servers[hostname] = AuthoritativeDNSServer(hostname, zones)

    # ── helper: get TLD server ──────────────────────────────
    def _get_tld_server(self, tld: str) -> TLDDNSServer:
        return self._tld_servers.get(tld)

    # ── helper: get authoritative server by hostname ────────
    def _get_auth_server(self, hostname: str) -> AuthoritativeDNSServer:
        return self._auth_servers.get(hostname)

    # ── helper: extract TLD ─────────────────────────────────
    @staticmethod
    def _tld_of(domain: str) -> str:
        return '.' + domain.rstrip('.').split('.')[-1]

    # ────────────────────────────────────────────────────────
    # PUBLIC: resolve a domain name
    # mode = "recursive" or "iterative"
    # ────────────────────────────────────────────────────────
    def resolve(self, domain: str, mode: str = "recursive") -> DNSMessage:
        domain = domain.lower().rstrip('.')
        print(f"\n{'='*64}")
        print(f"  LOCAL RESOLVER [{self.name}]  Query: '{domain}'  Mode: {mode.upper()}")
        print(f"{'='*64}")

        # Step 1: Check local cache first
        cached = self.cache.get(domain)
        if cached:
            print(f"\n  [CACHE HIT] '{domain}' found in cache — no upstream query needed!")
            flags_r = DNSFlags(qr=1, ra=1, rcode=0)
            return DNSMessage(_new_id(), flags_r,
                              questions=[(domain, "A")],
                              answers=cached)

        print(f"\n  [CACHE MISS] '{domain}' not in cache — starting {mode} resolution...")

        if mode == "recursive":
            return self._recursive_resolve(domain)
        else:
            return self._iterative_resolve(domain)

    # ────────────────────────────────────────────────────────
    # RECURSIVE (sequential) resolution — LEFT side of Fig 01
    # The local server contacts root, root returns TLD name,
    # local server contacts TLD, TLD returns auth name,
    # local server contacts auth, auth returns final answer.
    # Messages: 1→local, 2→root, 3←root, 4→TLD, 5←TLD, 6→auth, 7←auth, 8→client
    # ────────────────────────────────────────────────────────
    def _recursive_resolve(self, domain: str) -> DNSMessage:
        print("\n  ── Recursive resolution (local server does all hops) ──")
        step = 1

        # Step 1: client → local (already here, just log)
        print(f"\n  Step {step}: Client → Local DNS  (query '{domain}')")
        step += 1

        # Step 2: local → Root
        print(f"\n  Step {step}: Local → Root DNS Server")
        step += 1
        root_response = self.root.query(domain)
        print(f"  Step {step}: Root → Local  (referral to TLD)")
        step += 1
        self._print_message("Root Response", root_response)

        if root_response.flags.rcode != 0:
            return root_response

        tld_server_name = root_response.authority[0].value
        tld             = self._tld_of(domain)
        tld_server      = self._get_tld_server(tld)

        if not tld_server:
            print(f"  [ERROR] No TLD server for {tld}")
            return root_response

        # Step 4: local → TLD
        print(f"\n  Step {step}: Local → TLD DNS Server ({tld_server_name})")
        step += 1
        tld_response = tld_server.query(domain)
        print(f"  Step {step}: TLD → Local  (referral to authoritative)")
        step += 1
        self._print_message("TLD Response", tld_response)

        if tld_response.flags.rcode != 0:
            return tld_response

        auth_server_name = tld_response.authority[0].value
        auth_server      = self._get_auth_server(auth_server_name)

        if not auth_server:
            print(f"  [ERROR] Unknown authoritative server: {auth_server_name}")
            return tld_response

        # Step 6: local → Authoritative
        print(f"\n  Step {step}: Local → Authoritative DNS Server ({auth_server_name})")
        step += 1
        auth_response = auth_server.query(domain)
        print(f"  Step {step}: Authoritative → Local  (final answer)")
        step += 1
        self._print_message("Authoritative Response", auth_response)

        # Cache the result
        if auth_response.answers:
            self.cache.put(domain, auth_response.answers,
                           ttl=auth_response.answers[0].ttl)
            print(f"\n  [CACHE] Stored '{domain}' → {len(auth_response.answers)} record(s)")

        # Step 8: local → client
        print(f"\n  Step {step}: Local → Client  (final answer delivered)")
        return auth_response

    # ────────────────────────────────────────────────────────
    # ITERATIVE resolution — RIGHT side of Fig 01
    # Each server tells the local resolver WHO to ask next.
    # Local resolver makes each hop itself.
    # Messages: 1→local, 2→root, 3←root, 4→TLD(direct), 5←TLD,
    #           6→auth(direct), 7←auth, 8→client
    # ────────────────────────────────────────────────────────
    def _iterative_resolve(self, domain: str) -> DNSMessage:
        print("\n  ── Iterative resolution (client-guided, server refers next hop) ──")

        print(f"\n  Step 1: Client → Local DNS  (query '{domain}')")
        print(f"\n  Step 2: Local → Root DNS  (ask who handles {self._tld_of(domain)})")

        root_response = self.root.query(domain)
        self._print_message("Root Response (referral)", root_response)
        if root_response.flags.rcode != 0:
            return root_response

        print(f"\n  Step 3: Root → Local  (here's the TLD server name)")

        tld = self._tld_of(domain)
        tld_server = self._get_tld_server(tld)

        print(f"\n  Step 4: Local → TLD DNS  (now asking TLD directly)")
        tld_response = tld_server.query(domain)
        self._print_message("TLD Response (referral)", tld_response)
        if tld_response.flags.rcode != 0:
            return tld_response

        print(f"\n  Step 5: TLD → Local  (here's the authoritative server name)")

        auth_server_name = tld_response.authority[0].value
        auth_server      = self._get_auth_server(auth_server_name)

        if not auth_server:
            print(f"  [ERROR] Unknown authoritative server: {auth_server_name}")
            return tld_response

        print(f"\n  Step 6: Local → Authoritative  (asking {auth_server_name} directly)")
        auth_response = auth_server.query(domain)
        self._print_message("Authoritative Response (final)", auth_response)

        print(f"\n  Step 7: Authoritative → Local  (final IP answer)")

        # Cache the result
        if auth_response.answers:
            self.cache.put(domain, auth_response.answers,
                           ttl=auth_response.answers[0].ttl)
            print(f"\n  [CACHE] Stored '{domain}' → {len(auth_response.answers)} record(s)")

        print(f"\n  Step 8: Local → Client  (final answer delivered)")
        return auth_response

    # ────────────────────────────────────────────────────────
    # PRETTY PRINTER — formats the result like the sample run
    # ────────────────────────────────────────────────────────
    @staticmethod
    def _print_message(label: str, msg: DNSMessage):
        print(f"\n  ┌── {label} ──")
        print(f"  │  ID: 0x{msg.msg_id:04X}  Flags: {msg.flags}")
        for rr in msg.answers:
            print(f"  │  Answer   : {rr}")
        for rr in msg.authority:
            print(f"  │  Authority: {rr}")
        print(f"  └{'─'*40}")

    @staticmethod
    def print_dns_info(msg: DNSMessage):
        """Print the final DNS information in the assignment's sample format."""
        if not msg.answers:
            print("\n  [No A records found]")
            return

        domain = msg.questions[0][0] if msg.questions else "unknown"
        first_ip = msg.answers[0].value

        print(f"\n{'─'*60}")
        print(f"  {domain}/{first_ip}")
        print(f"  -- DNS INFORMATION --")

        a_ips = [rr.value for rr in msg.answers if rr.rtype == "A"]
        if a_ips:
            print(f"  A: {', '.join(a_ips)}")

        ns_vals = [rr.value for rr in msg.authority if rr.rtype == "NS"]
        if ns_vals:
            print(f"  NS: {', '.join(ns_vals)}")

        mx_vals = [rr.value for rr in msg.additional if rr.rtype == "MX"]
        if mx_vals:
            print(f"  MX: {', '.join(mx_vals)}")

        print(f"{'─'*60}")


# ─────────────────────────────────────────────────────────────
# SECTION 7: DEMO / MAIN
# ─────────────────────────────────────────────────────────────

def separator(title: str):
    print(f"\n{'#'*64}")
    print(f"#  {title}")
    print(f"{'#'*64}")


def main():
    print("=" * 64)
    print("  DNS IMPLEMENTATION  –  CS3001 Computer Networks")
    print("  Assignment II  |  Spring 2026  |  FAST-NUCES Karachi")
    print("=" * 64)

    # Create the local DNS resolver with a small cache (capacity=3 to demo eviction)
    resolver = LocalDNSServer(name="dns.poly.edu", cache_capacity=3)

    # ── Demo 1: Recursive resolution (sequential, left side of Fig 01) ────
    separator("DEMO 1: Recursive (Sequential) Resolution — google.com")
    response = resolver.resolve("google.com", mode="recursive")
    LocalDNSServer.print_dns_info(response)

    # ── Demo 2: Same domain again — should hit cache ──────────────────────
    separator("DEMO 2: Cache HIT — google.com (second query, no upstream needed)")
    response2 = resolver.resolve("google.com", mode="recursive")
    LocalDNSServer.print_dns_info(response2)

    # ── Demo 3: Iterative resolution (right side of Fig 01) ──────────────
    separator("DEMO 3: Iterative Resolution — github.com")
    response3 = resolver.resolve("github.com", mode="iterative")
    LocalDNSServer.print_dns_info(response3)

    # ── Demo 4: Another domain ────────────────────────────────────────────
    separator("DEMO 4: Iterative Resolution — youtube.com")
    response4 = resolver.resolve("youtube.com", mode="iterative")
    LocalDNSServer.print_dns_info(response4)

    # ── Demo 5: Cache overflow → LRU auto-flush ───────────────────────────
    separator("DEMO 5: Cache Auto-Flush (LRU Eviction) — nu.edu.pk")
    print("\n  Cache is now at capacity (3/3). Querying nu.edu.pk will evict oldest entry.")
    response5 = resolver.resolve("nu.edu.pk", mode="recursive")
    LocalDNSServer.print_dns_info(response5)

    # ── Demo 6: cs.umass.edu (from Figure 01) ─────────────────────────────
    separator("DEMO 6: Recursive Resolution — cs.umass.edu (from Fig 01)")
    response6 = resolver.resolve("cs.umass.edu", mode="recursive")
    LocalDNSServer.print_dns_info(response6)

    # ── Show final cache state ─────────────────────────────────────────────
    separator("FINAL CACHE STATE")
    resolver.cache.show()

    # ── DNS message header binary demo ────────────────────────────────────
    separator("DNS MESSAGE HEADER BINARY ENCODING (Fig 02)")
    query_flags = DNSFlags(qr=0, rd=1)  # Query with recursion desired
    reply_flags = DNSFlags(qr=1, ra=1, aa=1)  # Authoritative reply
    msg_id = 0xABCD

    query_msg = DNSMessage(msg_id, query_flags,
                           questions=[("google.com", "A")])
    reply_msg = DNSMessage(msg_id, reply_flags,
                           questions=[("google.com", "A")],
                           answers=[ResourceRecord("google.com", "A", "64.233.187.99")])

    print(f"\n  Query message header (hex): {query_msg.encode_header().hex()}")
    print(f"  ID=0x{msg_id:04X}  Flags=0x{query_flags.encode():04X}  {query_flags}")
    print(f"\n  Reply message header (hex): {reply_msg.encode_header().hex()}")
    print(f"  ID=0x{msg_id:04X}  Flags=0x{reply_flags.encode():04X}  {reply_flags}")
    print(f"\n  (Same ID on query and reply confirms they are matched — Fig 02 requirement)")

    print("\n\n  Done. All demos complete.")


if __name__ == "__main__":
    main()
