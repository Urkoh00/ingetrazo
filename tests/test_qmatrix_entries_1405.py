# SPDX-License-Identifier: GPL-3.0-or-later
"""Matrix entries are read without calling the QMatrix4x4 (#1405).

``m(row, col)`` works on PySide6 6.11 but not on 6.10.0, which
requirements.txt admits (``PySide6>=6.7,<6.12``): there it raises "'QMatrix4x4'
object is not callable". Moving a textured group (``_remap_uvws``), copying
textured loose faces (``transformed_attrs``) and reading a part's points
(``core.parts``) all did it. The matrices below refuse the call, as 6.10.0
does, so these tests fail on any PySide6 if a call comes back.
"""
import numpy as np
import pytest
from PySide6.QtGui import QMatrix4x4, QVector3D

from core.group import _remap_uvws, matrix_entries, transformed_attrs
from core.mesh import Mesh


class UncallableMatrix(QMatrix4x4):
    """A QMatrix4x4 that behaves as on PySide6 6.10.0."""

    def __call__(self, *args):
        raise TypeError("'QMatrix4x4' object is not callable")

    def inverted(self):
        inverse, ok = super().inverted()
        return UncallableMatrix(inverse), ok


def _move():
    m = UncallableMatrix()
    m.translate(2.0, -1.0, 0.5)
    m.rotate(30.0, 0.0, 0.0, 1.0)
    return m


def test_entries_are_read_from_the_column_major_data():
    m = QMatrix4x4()
    m.translate(2.0, -1.0, 0.5)
    at = matrix_entries(m)
    assert (at(0, 3), at(1, 3), at(2, 3)) == pytest.approx((2.0, -1.0, 0.5))
    ours = np.array([[at(r, c) for c in range(4)] for r in range(4)])
    qt = np.array([[getattr(m.row(r), axis)() for axis in "xyzw"] for r in range(4)])
    assert np.allclose(ours, qt)


def _textured_face():
    mesh = Mesh()
    face = mesh.add_face([QVector3D(0, 0, 0), QVector3D(1, 0, 0), QVector3D(1, 1, 0)])
    face.attrs = {"texture": {"uvw": [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0]}}
    return mesh, face


def _uv(uvw, p):
    return (uvw[0] * p[0] + uvw[1] * p[1] + uvw[2] * p[2] + uvw[3],
            uvw[4] * p[0] + uvw[5] * p[1] + uvw[6] * p[2] + uvw[7])


def test_a_moved_textured_group_keeps_its_uvs():
    mesh, face = _textured_face()
    before = _uv(face.attrs["texture"]["uvw"], (1.0, 1.0, 0.0))
    m = _move()
    _remap_uvws(mesh, m)
    moved = m.map(QVector3D(1, 1, 0))
    after = _uv(face.attrs["texture"]["uvw"], (moved.x(), moved.y(), moved.z()))
    assert after == pytest.approx(before)


def test_copied_textured_faces_keep_their_uvs():
    _mesh, face = _textured_face()
    m = _move()
    out = transformed_attrs(face.attrs, m)
    moved = m.map(QVector3D(1, 1, 0))
    assert _uv(out["texture"]["uvw"], (moved.x(), moved.y(), moved.z())) == pytest.approx(
        _uv(face.attrs["texture"]["uvw"], (1.0, 1.0, 0.0)))


def test_matrix_entries_agree_with_qt_mapping():
    m = _move()
    at = matrix_entries(m)
    p = QVector3D(0.3, 0.7, 1.1)
    mapped = m.map(p)
    expected = np.array([[at(r, c) for c in range(4)] for r in range(3)]) @ [0.3, 0.7, 1.1, 1.0]
    assert expected == pytest.approx([mapped.x(), mapped.y(), mapped.z()], abs=1e-6)
