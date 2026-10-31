"""按线路节点顺序计算球面折线长度，单位米。"""

import math

EARTH_RADIUS_M = 6371008.8


def route_length_metres(devices) -> str:
    ordered = sorted(devices, key=lambda item: item.get("sequence", 0))
    if len(ordered) < 2:
        return "0.000"
    coordinates = [item.get("coordinate") for item in ordered]
    for point in coordinates:
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in point)
                or abs(point[0]) > 180 or abs(point[1]) > 90 or tuple(point) == (0, 0)):
            return ""
    total = 0.0
    for left, right in zip(coordinates, coordinates[1:]):
        lng1, lat1, lng2, lat2 = map(math.radians, (*left, *right))
        a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
        total += 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, max(0.0, a))))
    return f"{total:.3f}"
