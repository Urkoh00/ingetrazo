# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""IFC export of the design data a tag carries (#1222): GlobalId, property
sets, quantities, predefined types and text attributes.

An extension that knows an element's data (an architecture plug-in, a
structural tool…) puts it in the tag; downstream tools — energy, structural
and MEP analysis — read it from the IFC. A tag with only class and name must
export exactly as before.
"""
from __future__ import annotations

import re

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QVector3D

from core.bim import next_object_id, tag_faces, tag_group
from core.edits import build_add_edges
from core.history import AddFaceCommand, History, MakeGroupCommand
from core.scene import Scene
from formats.ifc import save_ifc
from tools.base import ToolContext
from tools.pushpull import PushPullTool

GUID = "3O8ck_2m1Db9UVn58hq95l"


class _Vp:
    def __init__(self, scene):
        self.scene = scene
        self.history = History(scene)

    def update(self):
        pass

    def set_hover(self, *_):
        pass

    def set_suppressed_faces(self, *_):
        pass

    def flash_status(self, *a, **k):
        pass


def V(x, y, z=0.0):
    return QVector3D(x, y, z)


def _box_group(scene, vp, x0, length=4.0, width=0.3, height=2.7):
    """A watertight box made a group: the usual shape of an extension's
    element."""
    pts = [V(x0, 0), V(x0 + length, 0), V(x0 + length, width), V(x0, width)]
    n0 = len(scene.mesh.faces)
    vp.history.execute(build_add_edges(
        scene, [(pts[i], pts[(i + 1) % 4]) for i in range(4)],
        detect_faces=False, extra=[AddFaceCommand(list(pts))]))
    pp = PushPullTool()
    pp.hovered_face = scene.mesh.faces[-1]
    pp._hover_group = None
    pp.on_click(ToolContext(viewport=vp, world=pts[0], screen=QPointF(0, 0),
                            modifiers=Qt.NoModifier, snap=None))
    pp.extrusion = height
    pp._commit(vp)
    vp.history.execute(MakeGroupCommand(scene.mesh.faces[n0:], []))
    return scene.groups[-1]


def _party_wall_scene(**extra):
    """One wall group tagged like a party wall, with ``extra`` tag keys."""
    scene = Scene()
    vp = _Vp(scene)
    g = _box_group(scene, vp, 0.0)
    tag_group(g, "IfcWall", "M2")
    g.ifc.update({
        "guid": GUID,
        "psets": {"Pset_WallCommon": {"IsExternal": True,
                                      "Reference": "Medianera 30"},
                  "Custom_Data": {"Layers": 3, "Thickness": 0.3,
                                  "Unsupported": [1, 2]}},
        "quantities": {"Width": 0.3, "NetVolume": 3.0},
        "enums": {"PredefinedType": "USERDEFINED"},
        "attributes": {"ObjectType": "PARTYWALL",
                       "Description": "Medianera con el vecino"},
    })
    g.ifc.update(extra)
    return scene


def _export(tmp_path, scene, name="m.ifc"):
    p = tmp_path / name
    save_ifc(scene, p)
    return p, p.read_text()


def _element(text, cls):
    return re.search(rf"#(\d+)={cls}\((.*)\);", text)


# --- plain tags are unchanged ------------------------------------------------

def test_a_plain_tag_writes_no_property_set(tmp_path):
    scene = Scene()
    vp = _Vp(scene)
    tag_group(_box_group(scene, vp, 0.0), "IfcWall", "M1")
    _, text = _export(tmp_path, scene)
    assert "IFCPROPERTYSET(" not in text
    attrs = _element(text, "IFCWALL").group(2).split(",")
    assert attrs[4] == "$" and attrs[8] == "$"      # ObjectType, PredefinedType


# --- GlobalId --------------------------------------------------------------------

def test_the_tag_guid_is_the_global_id(tmp_path):
    _, text = _export(tmp_path, _party_wall_scene())
    assert _element(text, "IFCWALL").group(2).startswith(f"'{GUID}',")


def test_a_copied_guid_is_not_repeated(tmp_path):
    scene = _party_wall_scene()
    vp = _Vp(scene)
    copy = _box_group(scene, vp, 10.0)
    tag_group(copy, "IfcWall", "M2 copy")
    copy.ifc["guid"] = GUID                         # what a copy would carry
    _, text = _export(tmp_path, scene)
    guids = re.findall(r"=IFCWALL\('([^']+)'", text)
    assert len(guids) == 2 and len(set(guids)) == 2 and GUID in guids


def test_a_malformed_guid_is_replaced(tmp_path):
    _, text = _export(tmp_path, _party_wall_scene(guid="not-a-guid"))
    guid = re.search(r"=IFCWALL\('([^']+)'", text).group(1)
    assert len(guid) == 22 and guid != "not-a-guid"


# --- property sets ---------------------------------------------------------

def test_property_sets_are_written_with_typed_values(tmp_path):
    _, text = _export(tmp_path, _party_wall_scene())
    assert "IFCPROPERTYSINGLEVALUE('IsExternal',$,IFCBOOLEAN(.T.),$)" in text
    assert "IFCPROPERTYSINGLEVALUE('Reference',$,IFCLABEL('Medianera 30'),$)" \
        in text
    assert "IFCPROPERTYSINGLEVALUE('Layers',$,IFCINTEGER(3),$)" in text
    assert "IFCPROPERTYSINGLEVALUE('Thickness',$,IFCREAL(0.300000),$)" in text
    assert "'Unsupported'" not in text              # no IFC type: skipped
    assert "'Pset_WallCommon'" in text and "'Custom_Data'" in text
    wall = _element(text, "IFCWALL").group(1)
    rels = re.findall(rf"IFCRELDEFINESBYPROPERTIES\([^;]*\(#{wall}\),#\d+\);",
                      text)
    assert len(rels) == 3                           # 2 psets + the Qto set


# --- quantities ----------------------------------------------------------------

def test_tag_quantities_go_into_the_qto_set(tmp_path):
    _, text = _export(tmp_path, _party_wall_scene())
    q = {m.group(1): float(m.group(2)) for m in re.finditer(
        r"IFCQUANTITY\w+\('(\w+)',\$,\$,([\d.]+),", text)}
    assert q["NetVolume"] == pytest.approx(3.0)     # new name: appended
    assert q["Width"] == pytest.approx(0.3)         # the tag's value
    assert q["Length"] == pytest.approx(4.0)        # measured, untouched
    assert "IFCQUANTITYVOLUME('NetVolume'" in text  # kind read from the name


# --- predefined types and text attributes ------------------------------------

def test_userdefined_type_with_its_object_type(tmp_path):
    _, text = _export(tmp_path, _party_wall_scene())
    attrs = _element(text, "IFCWALL").group(2).split(",")
    assert attrs[3] == "'Medianera con el vecino'"  # Description
    assert attrs[4] == "'PARTYWALL'"                # ObjectType
    assert attrs[8] == ".USERDEFINED."              # PredefinedType


def test_userdefined_without_object_type_becomes_notdefined(tmp_path):
    scene = _party_wall_scene(attributes={})
    _, text = _export(tmp_path, scene)
    assert _element(text, "IFCWALL").group(2).split(",")[8] == ".NOTDEFINED."


@pytest.mark.parametrize("value, written", [
    ("PARTITIONING", ".PARTITIONING."),
    ("partitioning", ".PARTITIONING."),
    ("BASESLAB", "$"),                              # a slab value, not a wall's
    ("NOT VALID", "$"),
])
def test_only_values_of_the_class_enumeration_are_written(tmp_path, value,
                                                          written):
    scene = _party_wall_scene(enums={"PredefinedType": value})
    _, text = _export(tmp_path, scene)
    assert _element(text, "IFCWALL").group(2).split(",")[8] == written


def test_door_operation_and_window_partitioning_slots(tmp_path):
    scene = Scene()
    door = scene.mesh.add_face([V(0, 0, 0), V(0.9, 0, 0),
                                V(0.9, 0, 2.1), V(0, 0, 2.1)])
    tag_faces([door], "IfcDoor", "P1", next_object_id(scene))
    door.attrs["ifc"].update(enums={"PredefinedType": "DOOR",
                                    "OperationType": "SINGLE_SWING_LEFT"})
    win = scene.mesh.add_face([V(3, 0, 1), V(4.2, 0, 1),
                               V(4.2, 0, 2.2), V(3, 0, 2.2)])
    tag_faces([win], "IfcWindow", "V1", next_object_id(scene))
    win.attrs["ifc"].update(
        enums={"PredefinedType": "WINDOW", "PartitioningType": "USERDEFINED"},
        attributes={"UserDefinedPartitioningType": "OSCILOBATIENTE"})
    _, text = _export(tmp_path, scene)
    d = _element(text, "IFCDOOR").group(2).split(",")
    assert (d[10], d[11]) == (".DOOR.", ".SINGLE_SWING_LEFT.")
    wv = _element(text, "IFCWINDOW").group(2).split(",")
    assert (wv[10], wv[11], wv[12]) == (".WINDOW.", ".USERDEFINED.",
                                        "'OSCILOBATIENTE'")


def test_space_long_name_and_type(tmp_path):
    scene = Scene()
    vp = _Vp(scene)
    room = _box_group(scene, vp, 0.0, length=3.0, width=2.5, height=2.7)
    tag_group(room, "IfcSpace", "R1")
    room.ifc.update(enums={"PredefinedType": "INTERNAL"},
                    attributes={"LongName": "Salón", "Tag": "ignored"},
                    psets={"Pset_SpaceCommon": {"IsExternal": False}})
    _, text = _export(tmp_path, scene)
    attrs = _element(text, "IFCSPACE").group(2).split(",")
    assert attrs[7] == "'Sal\\X2\\00F3\\X0\\n'"     # LongName, not Tag
    assert attrs[9] == ".INTERNAL."


# --- read back by an independent IFC implementation ---------------------------

def test_consumer_reads_the_tag_data_and_the_file_validates(tmp_path):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    import ifcopenshell.util.element as U
    import ifcopenshell.validate

    scene = _party_wall_scene()
    vp = _Vp(scene)
    slab = _box_group(scene, vp, 0.0, length=4.0, width=3.0, height=0.25)
    tag_group(slab, "IfcSlab", "S1")
    slab.ifc.update(enums={"PredefinedType": "BASESLAB"},
                    psets={"Pset_SlabCommon": {"IsExternal": True}})
    p, _ = _export(tmp_path, scene, "check.ifc")

    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(str(p), logger, express_rules=True)
    assert logger.statements == []

    f = ifcopenshell.open(str(p))
    (wall,) = f.by_type("IfcWall")
    assert wall.GlobalId == GUID
    assert (wall.PredefinedType, wall.ObjectType) == ("USERDEFINED",
                                                       "PARTYWALL")
    psets = U.get_psets(wall)
    assert psets["Pset_WallCommon"]["IsExternal"] is True
    assert psets["Qto_WallBaseQuantities"]["NetVolume"] == pytest.approx(3.0)
    (s,) = f.by_type("IfcSlab")
    assert s.PredefinedType == "BASESLAB"
    assert U.get_psets(s)["Pset_SlabCommon"]["IsExternal"] is True
