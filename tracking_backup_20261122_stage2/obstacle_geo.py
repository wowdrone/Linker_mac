"""Local course geometry shared by the relay, receiver and test tools."""
import csv
import json
import math
from dataclasses import dataclass

from guide_protocol import checked_lat_lon

EARTH_M = 6371008.8


def local_xy(lat, lon, origin):
    lat, lon = checked_lat_lon(lat, lon)
    lat0, lon0 = origin
    dlon = (lon - lon0 + 180) % 360 - 180
    return (math.radians(dlon) * EARTH_M * math.cos(math.radians(lat0)),
            math.radians(lat - lat0) * EARTH_M)


def from_xy(east, north, origin):
    lat0, lon0 = origin
    cos_lat = math.cos(math.radians(lat0))
    if abs(cos_lat) < 1e-6:
        raise ValueError("Local projection undefined near a pole")
    return checked_lat_lon(lat0 + math.degrees(north / EARTH_M),
                           (lon0 + math.degrees(east / (EARTH_M * cos_lat)) + 180) % 360 - 180)


def distance_m(lat1, lon1, lat2, lon2):
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    delta = math.radians(lon2 - lon1)
    a = math.sin((lat2 - lat1) / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta / 2)**2
    return 2 * EARTH_M * math.asin(math.sqrt(min(1, max(0, a))))


@dataclass(frozen=True)
class Station:
    s: float
    lateral: float
    segment: int
    distance: float


class Course:
    def __init__(self, points):
        self.points = [checked_lat_lon(*point) for point in points]
        if len(self.points) < 2:
            raise ValueError("Course requires at least two points")
        self.origin = self.points[0]
        self.xy = [local_xy(*point, self.origin) for point in self.points]
        self.segments = []
        self.length = 0.0
        self.closed = distance_m(*self.points[0], *self.points[-1]) < 0.5
        for (x, y), (x2, y2) in zip(self.xy, self.xy[1:]):
            dx, dy = x2 - x, y2 - y
            length = math.hypot(dx, dy)
            if length > 0.01:
                self.segments.append((self.length, x, y, dx, dy, length))
                self.length += length
        if not self.segments:
            raise ValueError("Course contains no usable segments")

    @classmethod
    def load(cls, path):
        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as file:
                return cls((row["lat"], row["lon"]) for row in csv.DictReader(file))
        geo = json.loads(path.read_text(encoding="utf-8"))
        if geo["type"] == "Feature":
            geo = geo["geometry"]
        if geo["type"] != "LineString":
            raise ValueError("Expected a GeoJSON LineString")
        return cls((point[1], point[0]) for point in geo["coordinates"])

    def project(self, lat, lon, hint_s=None):
        east, north = local_xy(lat, lon, self.origin)
        candidates = []
        for index, (start, x, y, dx, dy, length) in enumerate(self.segments):
            fraction = max(0, min(1, ((east - x) * dx + (north - y) * dy) / length**2))
            ex, ey = east - x - fraction * dx, north - y - fraction * dy
            s = start + length * fraction
            lateral = (dy * ex - dx * ey) / length
            error = math.hypot(ex, ey)
            distance_from_hint = abs(self.delta(s, hint_s)) if hint_s is not None else 0
            candidates.append((error, distance_from_hint, Station(s, lateral, index, error)))
        nearest = min(item[0] for item in candidates)
        if hint_s is None:
            return min(candidates, key=lambda item: item[0])[2]
        # At intersections, continuity resolves only similarly close branches.
        return min((item for item in candidates if item[0] <= nearest + 2),
                   key=lambda item: (item[1], item[0]))[2]

    def delta(self, target_s, vehicle_s):
        remaining = target_s - vehicle_s
        if self.closed:
            return (remaining + self.length / 2) % self.length - self.length / 2
        return remaining

    def point_at(self, s):
        s = s % self.length if self.closed else min(self.length, max(0, s))
        for start, x, y, dx, dy, length in self.segments:
            if s <= start + length:
                fraction = (s - start) / length
                return from_xy(x + fraction * dx, y + fraction * dy, self.origin)
        return self.points[-1]
