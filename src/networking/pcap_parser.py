"""Read a tcpdump capture and measure what the network actually carried."""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from scapy.all import IP, TCP, UDP, IPv6, PcapReader

WEB_PORTS = {80, 443}
SLACK_S = 1.0


def parse_sni(record: bytes) -> str | None:
    """Extract the server name from a complete TLS ClientHello record, or None."""
    try:
        if record[0] != 0x16 or record[5] != 0x01:  # handshake record, ClientHello
            return None
        p = 9 + 2 + 32
        p += 1 + record[p]                          # session id
        p += 2 + int.from_bytes(record[p:p + 2], "big")  # cipher suites
        p += 1 + record[p]                          # compression methods
        end = p + 2 + int.from_bytes(record[p:p + 2], "big")
        p += 2
        while p + 4 <= end:
            etype = int.from_bytes(record[p:p + 2], "big")
            elen = int.from_bytes(record[p + 2:p + 4], "big")
            if etype == 0:                          # server_name extension
                nlen = int.from_bytes(record[p + 7:p + 9], "big")
                return record[p + 9:p + 9 + nlen].decode("ascii", "replace")
            p += 4 + elen
    except IndexError:
        return None
    return None


@dataclass
class Flow:
    """One TCP connection (client ip/port -> server ip/port)."""
    client: tuple
    server: tuple
    first_ts: float
    last_ts: float = 0.0
    packets: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    syn: int = 0
    fin: int = 0
    rst: int = 0
    retrans: int = 0
    out_of_order: int = 0
    client_hellos: int = 0
    sni: str | None = None
    _hello_buf: bytes = b""
    _hello_done: bool = False
    _ranges: dict = field(default_factory=lambda: {"up": [], "down": []})  # merged [start, end) byte ranges seen


def _ip_layer(pkt):
    if pkt.haslayer(IP):
        return pkt[IP], pkt[IP].src, pkt[IP].dst
    if pkt.haslayer(IPv6):
        return pkt[IPv6], pkt[IPv6].src, pkt[IPv6].dst
    return None, None, None


def _classify_segment(flow: Flow, direction: str, seq: int, plen: int) -> str | None:
    """Classify a data segment against the byte ranges already seen in this direction."""
    if plen == 0:
        return None
    start, end = seq, seq + plen
    ranges = flow._ranges[direction]
    if any(a <= start and end <= b for a, b in ranges):
        return "retrans"
    kind = "out_of_order" if ranges and start < max(b for _, b in ranges) else None
    merged: list[list[int]] = []
    for a, b in sorted(ranges + [[start, end]]):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    flow._ranges[direction] = merged
    return kind


def parse_pcap(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (tcp_flows, packets) DataFrames. `packets` is a light per-packet table for time-window analysis."""
    flows: dict[tuple, Flow] = {}
    pkt_rows = []
    for pkt in PcapReader(str(path)):
        ip, src, dst = _ip_layer(pkt)
        if ip is None:
            continue
        ts, size = float(pkt.time), len(ip)
        if pkt.haslayer(TCP):
            tcp = pkt[TCP]
            if tcp.dport in WEB_PORTS:
                key, direction = (src, tcp.sport, dst, tcp.dport), "up"
            elif tcp.sport in WEB_PORTS:
                key, direction = (dst, tcp.dport, src, tcp.sport), "down"
            else:
                continue
            f = flows.get(key)
            flags = int(tcp.flags)
            is_syn = bool(flags & 0x02) and not (flags & 0x10)
            if f is None or (is_syn and f.packets > 0 and f.fin):  # port reused for a new connection
                f = flows[key] = Flow(client=key[:2], server=key[2:], first_ts=ts)
            payload = bytes(tcp.payload)
            f.packets += 1
            f.last_ts = ts
            if direction == "up":
                f.bytes_up += size
            else:
                f.bytes_down += size
            f.syn += is_syn
            f.fin += bool(flags & 0x01)
            f.rst += bool(flags & 0x04)
            kind = _classify_segment(f, direction, tcp.seq, len(payload))
            retrans, ooo = kind == "retrans", kind == "out_of_order"
            f.retrans += retrans
            f.out_of_order += ooo
            if direction == "up" and payload and not f._hello_done and not retrans:
                f._hello_buf += payload
                b = f._hello_buf
                if b[0] != 0x16:
                    f._hello_done = True
                elif len(b) >= 5 and len(b) >= 5 + int.from_bytes(b[3:5], "big"):
                    f._hello_done = True
                    if len(b) > 5 and b[5] == 0x01:
                        f.client_hellos += 1
                        f.sni = parse_sni(b)
            pkt_rows.append((ts, "tcp", direction, size, f"{key}", is_syn, retrans, ooo))
        elif pkt.haslayer(UDP):
            udp = pkt[UDP]
            if udp.dport == 443 or udp.sport == 443:
                direction = "up" if udp.dport == 443 else "down"
                pkt_rows.append((ts, "quic", direction, size, f"{(src, udp.sport, dst, udp.dport)}", False, False, False))
    flow_df = pd.DataFrame([{
        "client": f"{f.client[0]}:{f.client[1]}", "server": f"{f.server[0]}:{f.server[1]}", "sni": f.sni,
        "first_ts": f.first_ts, "last_ts": f.last_ts, "duration_s": round(f.last_ts - f.first_ts, 3),
        "packets": f.packets, "bytes_up": f.bytes_up, "bytes_down": f.bytes_down,
        "syn": f.syn, "fin": f.fin, "rst": f.rst, "retransmissions": f.retrans, "out_of_order": f.out_of_order,
        "tls_client_hellos": f.client_hellos,
    } for f in flows.values()])
    pkts = pd.DataFrame(pkt_rows, columns=["ts", "proto", "direction", "bytes", "flow", "syn", "retrans", "ooo"])
    return flow_df, pkts


def per_run(flows: pd.DataFrame, pkts: pd.DataFrame, runs: pd.DataFrame) -> pd.DataFrame:
    """Network metrics for each benchmark run's time window."""
    out = []
    for _, r in runs.dropna(subset=["start_epoch"]).iterrows():
        lo, hi = r["start_epoch"], r["end_epoch"] + SLACK_S
        p = pkts[(pkts.ts >= lo) & (pkts.ts <= hi)]
        fl = flows[(flows.first_ts >= lo) & (flows.first_ts <= hi)]  # connections *opened* during the run
        out.append({
            "experiment_id": r["experiment_id"], "method": r["method"], "round": r["round"],
            "pcap_packets": len(p), "pcap_kb_total": round(p.bytes.sum() / 1024, 1),
            "pcap_kb_down": round(p[p.direction == "down"].bytes.sum() / 1024, 1),
            "pcap_kb_up": round(p[p.direction == "up"].bytes.sum() / 1024, 1),
            "tcp_connections": int(fl.syn.clip(upper=1).sum()),
            "tls_handshakes": int(fl.tls_client_hellos.sum()),
            "retransmissions": int(p.retrans.sum()),
            "out_of_order": int(p.ooo.sum()),
            "quic_packets": int((p.proto == "quic").sum()),
            "snis": ",".join(sorted(set(fl.sni.dropna()))),
        })
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcap", type=Path)
    ap.add_argument("--runs", type=Path, default=None, help="<bench_id>.runs.csv from the benchmark runner")
    args = ap.parse_args()
    flows, pkts = parse_pcap(args.pcap)
    out_dir = Path("experiments/metrics")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.pcap.stem
    flows.to_csv(out_dir / f"{stem}.flows.csv", index=False)
    print(f"{args.pcap}: {len(pkts)} web packets, {pkts.bytes.sum() / 1024:.1f} KB, {len(flows)} TCP flows, "
          f"{int(flows.tls_client_hellos.sum()) if len(flows) else 0} TLS handshakes, "
          f"{int(pkts.retrans.sum())} retransmissions, {int(pkts.ooo.sum())} out-of-order, "
          f"{int((pkts.proto == 'quic').sum())} QUIC packets")
    if len(flows):
        print(flows.groupby("sni", dropna=False)[["packets", "bytes_down", "bytes_up", "tls_client_hellos"]].sum()
              .sort_values("bytes_down", ascending=False).to_string())
    if args.runs:
        runs = pd.read_csv(args.runs)
        pr = per_run(flows, pkts, runs)
        merged = runs.merge(pr, on=["experiment_id", "method", "round"], how="left")
        merged.to_csv(out_dir / f"{stem}.per_run.csv", index=False)
        cols = ["method", "round", "n_requests", "bandwidth_kb", "pcap_kb_total", "pcap_packets",
                "tcp_connections", "tls_handshakes", "retransmissions", "out_of_order", "quic_packets"]
        print("\n" + merged[cols].to_string(index=False))
        agg = merged.groupby("method")[["bandwidth_kb", "pcap_kb_total", "pcap_packets", "tcp_connections",
                                        "tls_handshakes", "retransmissions", "out_of_order", "n_requests"]].mean().round(1)
        agg["requests_per_connection"] = (agg.n_requests / agg.tcp_connections.replace(0, float("nan"))).round(2)
        agg["pcap_overhead_pct"] = ((agg.pcap_kb_total / agg.bandwidth_kb - 1) * 100).round(1)
        agg.to_csv(out_dir / f"{stem}.per_method.csv")
        print("\nmean per method:\n" + agg.to_string())


if __name__ == "__main__":
    main()
