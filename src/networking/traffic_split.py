"""Split each benchmark run's captured traffic into *target* (Wikimedia) and *other* traffic."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path

import pandas as pd

from src.networking.pcap_parser import SLACK_S, parse_pcap

TARGET_SUFFIXES = ("wikipedia.org", "wikimedia.org")


def _server_ip(flow_key: str, proto: str) -> str:
    """packets.flow holds str(tuple). TCP key = (client, cport, server, sport); QUIC key = (src, sport, dst, dport)."""
    t = ast.literal_eval(flow_key)
    if proto == "tcp":
        return t[2]
    return t[2] if t[3] == 443 else t[0]


def split(flows: pd.DataFrame, pkts: pd.DataFrame, runs: pd.DataFrame) -> pd.DataFrame:
    is_target = flows.sni.fillna("").str.endswith(TARGET_SUFFIXES)
    target_ips = {s.rsplit(":", 1)[0] for s in flows.loc[is_target, "server"]}
    ip_to_sni = (flows.dropna(subset=["sni"]).assign(ip=lambda d: d.server.str.rsplit(":", n=1).str[0])
                 .groupby("ip").sni.first().to_dict())
    pkts = pkts.copy()
    pkts["server_ip"] = [_server_ip(f, p) for f, p in zip(pkts.flow, pkts.proto)]
    pkts["target"] = pkts.server_ip.isin(target_ips)
    pkts["host"] = pkts.server_ip.map(ip_to_sni).fillna(pkts.server_ip)
    rows = []
    for _, r in runs.dropna(subset=["start_epoch"]).iterrows():
        p = pkts[(pkts.ts >= r.start_epoch) & (pkts.ts <= r.end_epoch + SLACK_S)]
        t, o = p[p.target], p[~p.target]
        rows.append({
            "experiment_id": r.experiment_id, "method": r.method, "round": r["round"],
            "app_kb": r.bandwidth_kb,
            "target_kb": round(t.bytes.sum() / 1024, 1), "target_packets": len(t),
            "target_retrans": int(t.retrans.sum()),
            "target_out_of_order": int(t.ooo.sum()),
            "target_tcp_connections": int(t.syn.sum()),
            "other_kb": round(o.bytes.sum() / 1024, 1), "other_packets": len(o),
            "other_quic_kb": round(o[o.proto == "quic"].bytes.sum() / 1024, 1),
            "other_hosts": ", ".join(o.groupby("host").bytes.sum().sort_values(ascending=False)
                                     .head(4).index.astype(str)),
        })
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap", type=Path)
    ap.add_argument("--runs", type=Path, required=True)
    args = ap.parse_args()
    flows, pkts = parse_pcap(args.pcap)
    runs = pd.read_csv(args.runs)
    res = split(flows, pkts, runs)
    out = Path("experiments/metrics") / f"{args.pcap.stem}.traffic_split.csv"
    res.to_csv(out, index=False)
    pd.set_option("display.width", 250)
    print(res.drop(columns=["experiment_id"]).to_string(index=False))
    agg = res.groupby("method")[["app_kb", "target_kb", "target_packets", "target_retrans", "target_out_of_order",
                                 "target_tcp_connections", "other_kb", "other_quic_kb"]].agg(["mean", "std"]).round(1)
    print("\n" + agg.to_string())
    agg.to_csv(out.with_name(out.stem + "_per_method.csv"))
    first, last = runs.start_epoch.min(), runs.end_epoch.max() + SLACK_S
    print(f"\ncapture: {pkts.ts.min():.0f}..{pkts.ts.max():.0f}; benchmark window {first:.0f}..{last:.0f}; "
          f"packets outside benchmark window: {int(((pkts.ts < first) | (pkts.ts > last)).sum())}")


if __name__ == "__main__":
    main()
