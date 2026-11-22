"""pcap parser checked on a small synthetic capture (no real traffic needed)."""
from scapy.all import IP, TCP, UDP, Ether, Raw, wrpcap

from src.networking.pcap_parser import parse_pcap, parse_sni


def client_hello(sni: str) -> bytes:
    """Minimal TLS 1.2-style ClientHello record carrying one server_name extension."""
    name = sni.encode()
    sni_ext = (b"\x00\x00" + (len(name) + 5).to_bytes(2, "big") + (len(name) + 3).to_bytes(2, "big")
               + b"\x00" + len(name).to_bytes(2, "big") + name)
    pad_ext = b"\x00\x15" + (200).to_bytes(2, "big") + b"\x00" * 200  # makes it long enough to split
    exts = pad_ext + sni_ext
    body = (b"\x03\x03" + b"\x11" * 32 + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00"
            + len(exts).to_bytes(2, "big") + exts)
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs


def test_parse_sni():
    assert parse_sni(client_hello("en.wikipedia.org")) == "en.wikipedia.org"


def test_flows_handshakes_retransmissions_quic(tmp_path):
    c, s = "10.0.0.2", "185.15.58.224"
    hello = client_hello("en.wikipedia.org")
    part1, part2 = hello[:150], hello[150:]

    def up(seq, flags="A", data=b"", t=0.0):
        p = Ether() / IP(src=c, dst=s) / TCP(sport=50000, dport=443, seq=seq, flags=flags) / Raw(data)
        p.time = 1000 + t
        return p

    def down(seq, flags="A", data=b"", t=0.0):
        p = Ether() / IP(src=s, dst=c) / TCP(sport=443, dport=50000, seq=seq, flags=flags) / Raw(data)
        p.time = 1000 + t
        return p

    q = Ether() / IP(src=c, dst=s) / UDP(sport=50001, dport=443) / Raw(b"\xc0" + b"x" * 50)
    q.time = 1000.9
    pkts = [up(1, "S", t=0.0), down(100, "SA", t=0.01), up(2, "A", t=0.02),
            up(2, "PA", part1, 0.03), up(2 + len(part1), "PA", part2, 0.04),
            down(101, "PA", b"y" * 500, 0.1), down(101, "PA", b"y" * 500, 0.3),  # second one = retransmission
            down(1101, "PA", b"z" * 500, 0.35), down(601, "PA", b"w" * 500, 0.36),  # gap filled late = out-of-order
            up(2 + len(hello), "FA", t=0.5), q]
    path = tmp_path / "t.pcap"
    wrpcap(str(path), pkts)
    flows, p = parse_pcap(path)
    assert len(flows) == 1
    f = flows.iloc[0]
    assert f.syn == 1 and f.fin == 1 and f.tls_client_hellos == 1 and f.sni == "en.wikipedia.org"
    assert f.retransmissions == 1 and f.out_of_order == 1
    assert (p.proto == "quic").sum() == 1
