"""Dependency-light oriented-box geometry used by the quality audit."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence, Tuple


EPSILON = 1e-9
Point = Tuple[float, float]


def _finite_vector(value: Any, length: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) < length:
        raise ValueError(f"{label} must contain at least {length} values")
    result = tuple(float(item) for item in value[:length])
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{label} contains a non-finite value")
    return result


def oriented_rectangle(
    center: Sequence[float], extent: Sequence[float], yaw_degrees: float
) -> list[Point]:
    """Return a counter-clockwise rectangle; extents are CARLA half-sizes."""
    cx, cy = _finite_vector(center, 2, "center")
    ex, ey = _finite_vector(extent, 2, "extent")
    yaw = float(yaw_degrees)
    if ex <= 0 or ey <= 0 or not math.isfinite(yaw):
        raise ValueError("box extent must be positive and yaw must be finite")
    cosine = math.cos(math.radians(yaw))
    sine = math.sin(math.radians(yaw))
    corners = ((-ex, -ey), (ex, -ey), (ex, ey), (-ex, ey))
    return [
        (cx + cosine * x - sine * y, cy + sine * x + cosine * y)
        for x, y in corners
    ]


def polygon_area(points: Sequence[Point]) -> float:
    if len(points) < 3:
        return 0.0
    twice_area = 0.0
    for index, point in enumerate(points):
        following = points[(index + 1) % len(points)]
        twice_area += point[0] * following[1] - following[0] * point[1]
    return abs(twice_area) * 0.5


def _cross(left: Point, right: Point) -> float:
    return left[0] * right[1] - left[1] * right[0]


def _subtract(left: Point, right: Point) -> Point:
    return left[0] - right[0], left[1] - right[1]


def _inside(point: Point, start: Point, end: Point) -> bool:
    return _cross(_subtract(end, start), _subtract(point, start)) >= -EPSILON


def _line_intersection(
    segment_start: Point,
    segment_end: Point,
    clip_start: Point,
    clip_end: Point,
) -> Point:
    segment = _subtract(segment_end, segment_start)
    clip = _subtract(clip_end, clip_start)
    denominator = _cross(segment, clip)
    if abs(denominator) <= EPSILON:
        return segment_end
    offset = _subtract(clip_start, segment_start)
    ratio = _cross(offset, clip) / denominator
    return (
        segment_start[0] + ratio * segment[0],
        segment_start[1] + ratio * segment[1],
    )


def convex_intersection(
    subject_polygon: Sequence[Point], clip_polygon: Sequence[Point]
) -> list[Point]:
    """Clip one convex counter-clockwise polygon against another."""
    output = list(subject_polygon)
    for index, clip_start in enumerate(clip_polygon):
        clip_end = clip_polygon[(index + 1) % len(clip_polygon)]
        input_points = output
        output = []
        if not input_points:
            break
        previous = input_points[-1]
        for current in input_points:
            current_inside = _inside(current, clip_start, clip_end)
            previous_inside = _inside(previous, clip_start, clip_end)
            if current_inside:
                if not previous_inside:
                    output.append(
                        _line_intersection(previous, current, clip_start, clip_end)
                    )
                output.append(current)
            elif previous_inside:
                output.append(
                    _line_intersection(previous, current, clip_start, clip_end)
                )
            previous = current
    return output


def _axes(polygon: Sequence[Point]) -> list[Point]:
    axes = []
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        edge = _subtract(end, start)
        length = math.hypot(edge[0], edge[1])
        if length > EPSILON:
            axes.append((-edge[1] / length, edge[0] / length))
    return axes


def _projection(polygon: Sequence[Point], axis: Point) -> tuple[float, float]:
    values = [point[0] * axis[0] + point[1] * axis[1] for point in polygon]
    return min(values), max(values)


def sat_penetration(
    first: Sequence[Point], second: Sequence[Point]
) -> float:
    """Return minimum SAT penetration, or zero when polygons do not overlap."""
    minimum_overlap = math.inf
    for axis in [*_axes(first), *_axes(second)]:
        first_min, first_max = _projection(first, axis)
        second_min, second_max = _projection(second, axis)
        overlap = min(first_max, second_max) - max(first_min, second_min)
        if overlap <= EPSILON:
            return 0.0
        minimum_overlap = min(minimum_overlap, overlap)
    return 0.0 if minimum_overlap is math.inf else minimum_overlap


def _point_segment_distance(point: Point, start: Point, end: Point) -> float:
    edge = _subtract(end, start)
    denominator = edge[0] * edge[0] + edge[1] * edge[1]
    if denominator <= EPSILON:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    offset = _subtract(point, start)
    ratio = max(
        0.0,
        min(1.0, (offset[0] * edge[0] + offset[1] * edge[1]) / denominator),
    )
    nearest = (start[0] + ratio * edge[0], start[1] + ratio * edge[1])
    return math.hypot(point[0] - nearest[0], point[1] - nearest[1])


def polygon_clearance(first: Sequence[Point], second: Sequence[Point]) -> float:
    if polygon_area(convex_intersection(first, second)) > EPSILON:
        return 0.0
    distances = []
    for point in first:
        for index, start in enumerate(second):
            distances.append(
                _point_segment_distance(point, start, second[(index + 1) % len(second)])
            )
    for point in second:
        for index, start in enumerate(first):
            distances.append(
                _point_segment_distance(point, start, first[(index + 1) % len(first)])
            )
    return min(distances) if distances else math.inf


def box_geometry(box: Mapping[str, Any]) -> tuple[list[Point], float, float]:
    center = box.get("center", box.get("location"))
    extent = box.get("extent")
    rotation = box.get("rotation")
    cx, cy, cz = _finite_vector(center, 3, "box.center")
    ex, ey, ez = _finite_vector(extent, 3, "box.extent")
    if ex <= 0 or ey <= 0 or ez <= 0:
        raise ValueError("box extents must be positive")
    yaw = _finite_vector(rotation, 3, "box.rotation")[2]
    return oriented_rectangle((cx, cy), (ex, ey), yaw), cz - ez, cz + ez


def oriented_box_metrics(
    first_box: Mapping[str, Any], second_box: Mapping[str, Any]
) -> dict[str, Any]:
    first, first_z_min, first_z_max = box_geometry(first_box)
    second, second_z_min, second_z_max = box_geometry(second_box)
    intersection = convex_intersection(first, second)
    intersection_area = polygon_area(intersection)
    first_area = polygon_area(first)
    second_area = polygon_area(second)
    union = first_area + second_area - intersection_area
    z_overlap = min(first_z_max, second_z_max) - max(first_z_min, second_z_min)
    penetration = sat_penetration(first, second)
    positive_3d_overlap = intersection_area > EPSILON and z_overlap > EPSILON
    return {
        "bev_intersection_area_m2": intersection_area,
        "bev_iou": intersection_area / union if union > EPSILON else 0.0,
        "bev_penetration_m": penetration if positive_3d_overlap else 0.0,
        "bev_clearance_m": 0.0 if intersection_area > EPSILON else polygon_clearance(first, second),
        "z_overlap_m": z_overlap,
        "positive_3d_overlap": positive_3d_overlap,
        "intersection_polygon": [[x, y] for x, y in intersection],
    }
