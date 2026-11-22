"""Deterministic UDP loss/delay/outage proxy for loopback integration tests."""
import argparse
import heapq
import json
from pathlib import Path
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guide_protocol import endpoint, packets, udp


class LinkImpair:
    def __init__(self, routes, loss=0, delay=0, seed=1):
        self.loss, self.delay, self.random = loss, delay, random.Random(seed)
        self.cut = False
        self.sockets = []
        self.queue = []
        self.forwarded = self.dropped = self.serial = 0
        try:
            for route in routes:
                left, right, left_peer, right_peer = map(endpoint, route)
                if any(address[0] != "127.0.0.1" for address in (left, right, left_peer, right_peer)):
                    raise ValueError("Impairment proxy is loopback-only")
                first = udp(left)
                try:
                    second = udp(right)
                except BaseException:
                    first.close()
                    raise
                self.sockets += [(first, second, right_peer), (second, first, left_peer)]
        except BaseException:
            self.close()
            raise

    def poll(self):
        now = time.monotonic()
        for source, target, peer in self.sockets:
            for packet in packets(source, 281):
                if self.cut or self.random.random() < self.loss:
                    self.dropped += 1
                    continue
                self.serial += 1
                heapq.heappush(self.queue, (now + self.delay, self.serial, target, peer, packet))
        while self.queue and self.queue[0][0] <= now:
            _, _, sock, peer, packet = heapq.heappop(self.queue)
            if self.cut:
                self.dropped += 1
            else:
                sock.sendto(packet, peer)
                self.forwarded += 1

    def close(self):
        for source, _, _ in self.sockets:
            source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", required=True, help="JSON array of [left bind, right bind, left peer, right peer] per path")
    parser.add_argument("--loss", type=float, default=0)
    parser.add_argument("--delay-ms", type=float, default=0)
    parser.add_argument("--cut-after", type=float)
    parser.add_argument("--cut-duration", type=float, default=5)
    args = parser.parse_args()
    proxy = LinkImpair(json.loads(Path(args.routes).read_text()), args.loss, args.delay_ms / 1000)
    started = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - started
            proxy.cut = args.cut_after is not None and args.cut_after <= elapsed < args.cut_after + args.cut_duration
            proxy.poll()
            time.sleep(.005)
    except KeyboardInterrupt:
        print(f"forwarded={proxy.forwarded} dropped={proxy.dropped}")
    finally:
        proxy.close()


if __name__ == "__main__":
    main()
