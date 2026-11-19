# SPDX-License-Identifier: GPL-3.0-or-later
"""Curved walls get their Length, Width and side areas from their plan.

The wall quantities took the largest face as "the side". On a curved,
faceted wall the largest face is the top or the bottom, so a half-circle
wall of radius 1 m, 0.30 m thick and 2.70 m high came out 0.35 m long and
2.70 m wide. A prism of constant thickness has a top of area L·w and
perimeter 2L + 2w (2L for a closed ring), so the axis length and the
thickness follow from the top face. Straight walls keep the side measure.
"""
import math

import pytest
from PySide6.QtGui import QVector3D

from core.bim import class_quantities
from core.mesh import Mesh


def _point(radius, angle, z):
    return QVector3D(radius * math.cos(angle), radius * math.sin(angle), z)


def curved_wall(radius=1.0, width=0.3, height=2.7, sweep=math.pi, segments=48):
    """A faceted curved wall (``sweep`` = 2π: a closed ring)."""
    ring = math.isclose(sweep, 2 * math.pi)
    inner, outer = radius - width / 2, radius + width / 2
    angles = [sweep * i / segments for i in range(segments + (0 if ring else 1))]
    pairs = list(zip(angles, angles[1:] + ([angles[0]] if ring else [])))
    mesh = Mesh()
    for a, b in pairs:
        mesh.add_face([_point(outer, a, 0), _point(outer, b, 0),
                       _point(outer, b, height), _point(outer, a, height)])
        mesh.add_face([_point(inner, b, 0), _point(inner, a, 0),
                       _point(inner, a, height), _point(inner, b, height)])
    if ring:
        mesh.add_face([_point(outer, a, height) for a in angles],
                      [[_point(inner, a, height) for a in reversed(angles)]])
        mesh.add_face([_point(outer, a, 0) for a in reversed(angles)],
                      [[_point(inner, a, 0) for a in angles]])
    else:
        mesh.add_face([_point(outer, a, height) for a in angles]
                      + [_point(inner, a, height) for a in reversed(angles)])
        mesh.add_face([_point(inner, a, 0) for a in angles]
                      + [_point(outer, a, 0) for a in reversed(angles)])
        first, last = angles[0], angles[-1]
        mesh.add_face([_point(inner, first, 0), _point(outer, first, 0),
                       _point(outer, first, height), _point(inner, first, height)])
        mesh.add_face([_point(outer, last, 0), _point(inner, last, 0),
                       _point(inner, last, height), _point(outer, last, height)])
    return list(mesh.faces)


def _faceted_axis(radius, sweep, segments):
    return 2 * radius * segments * math.sin(sweep / segments / 2)


def _quantities(faces):
    _qset, entries, metrado = class_quantities("IfcWall", faces)
    return {name: value for _kind, name, value in entries}, metrado


@pytest.mark.parametrize("sweep", [math.pi / 2, math.pi, 2 * math.pi])
def test_a_curved_wall_measures_along_its_axis(sweep):
    values, metrado = _quantities(curved_wall(sweep=sweep))
    axis = _faceted_axis(1.0, sweep, 48)
    assert values["Height"] == pytest.approx(2.7)
    assert values["Length"] == pytest.approx(axis, rel=5e-3)
    assert values["Width"] == pytest.approx(0.3, rel=5e-3)
    assert values["GrossSideArea"] == pytest.approx(axis * 2.7, rel=5e-3)
    assert values["NetSideArea"] == pytest.approx(values["GrossSideArea"], rel=5e-3)
    assert metrado[0] == pytest.approx(values["NetSideArea"])


def test_a_straight_wall_keeps_the_side_face_measure():
    mesh = Mesh()
    p = [QVector3D(x, y, z) for z in (0, 2.7) for y in (0, 0.3) for x in (0, 4)]
    for a, b, c, d in [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4),
                       (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]:
        mesh.add_face([p[a], p[b], p[c], p[d]])
    values, _metrado = _quantities(list(mesh.faces))
    assert values["Length"] == pytest.approx(4.0)
    assert values["Width"] == pytest.approx(0.3)
    assert values["GrossSideArea"] == pytest.approx(10.8)
