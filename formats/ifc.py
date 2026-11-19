# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""IFC4 export — tagged BIM objects to a STEP Physical File, by hand.

Only what the thesis needs, written without dependencies: IFC4 is a text
format (ISO 10303-21) and our export scope is narrow — the spatial skeleton
(project → site → building → storey), one element per tagged BIM object with
its REAL IFC class, faceted-BRep geometry in world metres, and BaseQuantities
(area, and volume when the object is watertight) so downstream takeoff tools
read the numbers straight from the file. Untagged geometry is not exported —
the tag IS the opt-in, per the project's freeform-first principle.

ifcopenshell stays out deliberately: heavy native wheels lag Python releases
(the ARM/3.14 risk noted in CLAUDE.md), and import can be added later without
it constraining the writer.
"""
from __future__ import annotations

import datetime
import math
import uuid
from pathlib import Path

from core.bim import class_quantities, collect_objects, face_set_volume

_GUID_CHARS = ("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
               "abcdefghijklmnopqrstuvwxyz_$")

#: IFC4 total attribute counts per element class (the first 8 are the common
#: GlobalId..Tag layout; the rest are padded with ``$``). Classes with quirky
#: layouts (doors/windows carry OverallHeight/Width…, IfcSpace has LongName
#: where others have Tag) just get the right pad width.
_ATTR_COUNT = {"IfcPile": 10, "IfcDoor": 13, "IfcWindow": 13, "IfcSpace": 11,
               "IfcReinforcingBar": 14}
_DEFAULT_ATTRS = 9


def ifc_guid() -> str:
    """A 22-character IFC compressed GUID (base-64 over the IFC alphabet)."""
    n = uuid.uuid4().int
    return "".join(_GUID_CHARS[(n >> (6 * i)) & 63] for i in range(21, -1, -1))


def _s(text: str) -> str:
    """IFC string literal: apostrophes doubled, non-ASCII as \\X2\\…\\X0\\."""
    out = []
    run: list[str] = []

    def flush():
        if run:
            out.append("\\X2\\" + "".join(f"{ord(c):04X}" for c in run)
                       + "\\X0\\")
            run.clear()

    for ch in text or "":
        if ord(ch) < 128:
            flush()
            out.append("''" if ch == "'" else ch)
        else:
            run.append(ch)
    flush()
    return "'" + "".join(out) + "'"


def _f(v: float) -> str:
    return f"{v:.6f}"


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._next = 1

    def add(self, entity: str, *attrs) -> int:
        eid = self._next
        self._next += 1
        self.lines.append(f"#{eid}={entity}({','.join(attrs)});")
        return eid


def _brep(w: _Writer, faces, context_id: int) -> int:
    """Faceted geometry for a face set → IfcShapeRepresentation id. Closed
    sets become an IfcFacetedBrep; open ones an IfcShellBasedSurfaceModel."""
    point_ids: dict = {}

    def pt(v) -> int:
        key = (round(v.x(), 6), round(v.y(), 6), round(v.z(), 6))
        pid = point_ids.get(key)
        if pid is None:
            pid = w.add("IFCCARTESIANPOINT",
                        f"({_f(key[0])},{_f(key[1])},{_f(key[2])})")
            point_ids[key] = pid
        return pid

    face_ids = []
    for f in faces:
        bounds = []
        outer = w.add("IFCPOLYLOOP",
                      "(" + ",".join(f"#{pt(v.position)}" for v in f.loop)
                      + ")")
        bounds.append(f"#{w.add('IFCFACEOUTERBOUND', f'#{outer}', '.T.')}")
        for h in f.hole_loops:
            loop = w.add("IFCPOLYLOOP",
                         "(" + ",".join(f"#{pt(v.position)}" for v in h) + ")")
            bounds.append(f"#{w.add('IFCFACEBOUND', f'#{loop}', '.T.')}")
        face_ids.append(w.add("IFCFACE", "(" + ",".join(bounds) + ")"))

    face_list = "(" + ",".join(f"#{i}" for i in face_ids) + ")"
    closed = face_set_volume(faces) is not None
    if closed:
        shell = w.add("IFCCLOSEDSHELL", face_list)
        item = w.add("IFCFACETEDBREP", f"#{shell}")
        rep_type = "'Brep'"
    else:
        shell = w.add("IFCOPENSHELL", face_list)
        item = w.add("IFCSHELLBASEDSURFACEMODEL", f"(#{shell})")
        rep_type = "'SurfaceModel'"
    return w.add("IFCSHAPEREPRESENTATION", f"#{context_id}", "'Body'",
                 rep_type, f"(#{item})")


# --- What a tag may add to its element (#1222) ---------------------------
#
# Beyond class and name a tag (``group.ifc`` or ``face.attrs["ifc"]``) may
# carry the element's design data; each key is optional and ignored when
# absent or malformed, so a plain tag exports exactly as before:
#
#   "guid":       a 22-character IFC GlobalId, kept across exports
#   "psets":      {"Pset_WallCommon": {"IsExternal": True, ...}, ...}
#   "quantities": {"NetVolume": 4.3, ...} — into the class Qto set; a name
#                 the export also measures takes the tag's value
#   "enums":      {"PredefinedType": "PARTITIONING", "OperationType": ...}
#   "attributes": {"ObjectType": ..., "Description": ..., "Tag": ...,
#                  "LongName": ... (IfcSpace), "UserDefinedOperationType":
#                  ... (IfcDoor), "UserDefinedPartitioningType": ... (IfcWindow)}

#: IFC4 enumerations a tag may set, per class: (attribute position, values).
#: Generated from the IFC4 schema; a value outside its set is not written.
_ENUMS = {
    ("IfcBeam", "PredefinedType"): (8, frozenset("""
        BEAM JOIST HOLLOWCORE LINTEL SPANDREL T_BEAM USERDEFINED NOTDEFINED
        """.split())),
    ("IfcBuildingElementProxy", "PredefinedType"): (8, frozenset("""
        COMPLEX ELEMENT PARTIAL PROVISIONFORVOID PROVISIONFORSPACE USERDEFINED
        NOTDEFINED
        """.split())),
    ("IfcColumn", "PredefinedType"): (8, frozenset("""
        COLUMN PILASTER USERDEFINED NOTDEFINED
        """.split())),
    ("IfcCovering", "PredefinedType"): (8, frozenset("""
        CEILING FLOORING CLADDING ROOFING MOLDING SKIRTINGBOARD INSULATION
        MEMBRANE SLEEVING WRAPPING USERDEFINED NOTDEFINED
        """.split())),
    ("IfcDoor", "PredefinedType"): (10, frozenset("""
        DOOR GATE TRAPDOOR USERDEFINED NOTDEFINED
        """.split())),
    ("IfcDoor", "OperationType"): (11, frozenset("""
        SINGLE_SWING_LEFT SINGLE_SWING_RIGHT DOUBLE_DOOR_SINGLE_SWING
        DOUBLE_DOOR_SINGLE_SWING_OPPOSITE_LEFT
        DOUBLE_DOOR_SINGLE_SWING_OPPOSITE_RIGHT DOUBLE_SWING_LEFT
        DOUBLE_SWING_RIGHT DOUBLE_DOOR_DOUBLE_SWING SLIDING_TO_LEFT
        SLIDING_TO_RIGHT DOUBLE_DOOR_SLIDING FOLDING_TO_LEFT FOLDING_TO_RIGHT
        DOUBLE_DOOR_FOLDING REVOLVING ROLLINGUP SWING_FIXED_LEFT
        SWING_FIXED_RIGHT USERDEFINED NOTDEFINED
        """.split())),
    ("IfcFooting", "PredefinedType"): (8, frozenset("""
        CAISSON_FOUNDATION FOOTING_BEAM PAD_FOOTING PILE_CAP STRIP_FOOTING
        USERDEFINED NOTDEFINED
        """.split())),
    ("IfcMember", "PredefinedType"): (8, frozenset("""
        BRACE CHORD COLLAR MEMBER MULLION PLATE POST PURLIN RAFTER STRINGER
        STRUT STUD USERDEFINED NOTDEFINED
        """.split())),
    ("IfcPile", "PredefinedType"): (8, frozenset("""
        BORED DRIVEN JETGROUTING COHESION FRICTION SUPPORT USERDEFINED
        NOTDEFINED
        """.split())),
    ("IfcPile", "ConstructionType"): (9, frozenset("""
        CAST_IN_PLACE COMPOSITE PRECAST_CONCRETE PREFAB_STEEL USERDEFINED
        NOTDEFINED
        """.split())),
    ("IfcRailing", "PredefinedType"): (8, frozenset("""
        HANDRAIL GUARDRAIL BALUSTRADE USERDEFINED NOTDEFINED
        """.split())),
    ("IfcRamp", "PredefinedType"): (8, frozenset("""
        STRAIGHT_RUN_RAMP TWO_STRAIGHT_RUN_RAMP QUARTER_TURN_RAMP
        TWO_QUARTER_TURN_RAMP HALF_TURN_RAMP SPIRAL_RAMP USERDEFINED
        NOTDEFINED
        """.split())),
    ("IfcRoof", "PredefinedType"): (8, frozenset("""
        FLAT_ROOF SHED_ROOF GABLE_ROOF HIP_ROOF HIPPED_GABLE_ROOF GAMBREL_ROOF
        MANSARD_ROOF BARREL_ROOF RAINBOW_ROOF BUTTERFLY_ROOF PAVILION_ROOF
        DOME_ROOF FREEFORM USERDEFINED NOTDEFINED
        """.split())),
    ("IfcSlab", "PredefinedType"): (8, frozenset("""
        FLOOR ROOF LANDING BASESLAB USERDEFINED NOTDEFINED
        """.split())),
    ("IfcSpace", "PredefinedType"): (9, frozenset("""
        SPACE PARKING GFA INTERNAL EXTERNAL USERDEFINED NOTDEFINED
        """.split())),
    ("IfcStair", "PredefinedType"): (8, frozenset("""
        STRAIGHT_RUN_STAIR TWO_STRAIGHT_RUN_STAIR QUARTER_WINDING_STAIR
        QUARTER_TURN_STAIR HALF_WINDING_STAIR HALF_TURN_STAIR
        TWO_QUARTER_WINDING_STAIR TWO_QUARTER_TURN_STAIR
        THREE_QUARTER_WINDING_STAIR THREE_QUARTER_TURN_STAIR SPIRAL_STAIR
        DOUBLE_RETURN_STAIR CURVED_RUN_STAIR TWO_CURVED_RUN_STAIR USERDEFINED
        NOTDEFINED
        """.split())),
    ("IfcWall", "PredefinedType"): (8, frozenset("""
        MOVABLE PARAPET PARTITIONING PLUMBINGWALL SHEAR SOLIDWALL STANDARD
        POLYGONAL ELEMENTEDWALL USERDEFINED NOTDEFINED
        """.split())),
    ("IfcWindow", "PredefinedType"): (10, frozenset("""
        WINDOW SKYLIGHT LIGHTDOME USERDEFINED NOTDEFINED
        """.split())),
    ("IfcWindow", "PartitioningType"): (11, frozenset("""
        SINGLE_PANEL DOUBLE_PANEL_VERTICAL DOUBLE_PANEL_HORIZONTAL
        TRIPLE_PANEL_VERTICAL TRIPLE_PANEL_BOTTOM TRIPLE_PANEL_TOP
        TRIPLE_PANEL_LEFT TRIPLE_PANEL_RIGHT TRIPLE_PANEL_HORIZONTAL
        USERDEFINED NOTDEFINED
        """.split())),
}

#: Text attributes a tag may set: name -> position (IFC4 attribute order).
_TEXT_SLOTS = {"Description": 3, "ObjectType": 4, "Tag": 7}
_CLASS_TEXT_SLOTS = {
    "IfcSpace": {"Tag": None, "LongName": 7},
    "IfcDoor": {"UserDefinedOperationType": 12},
    "IfcWindow": {"UserDefinedPartitioningType": 12},
}
#: An enum set to USERDEFINED needs this text attribute, or it is NOTDEFINED.
_USERDEFINED_NEEDS = {"PredefinedType": "ObjectType",
                      "OperationType": "UserDefinedOperationType",
                      "PartitioningType": "UserDefinedPartitioningType"}

_Q_ENTITY = {"area": "IFCQUANTITYAREA", "volume": "IFCQUANTITYVOLUME",
             "length": "IFCQUANTITYLENGTH", "weight": "IFCQUANTITYWEIGHT",
             "count": "IFCQUANTITYCOUNT"}


def _is_guid(value) -> bool:
    """A well-formed compressed GlobalId: 22 characters of the IFC
    alphabet, the first one 0-3 (128 bits)."""
    return (isinstance(value, str) and len(value) == 22
            and value[0] in "0123" and all(c in _GUID_CHARS for c in value))


def _number(value):
    """``value`` as a finite float, or None (bools are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _quantity_kind(name: str) -> str:
    """The measure of a tag-supplied quantity, read from its standard name."""
    for word, kind in (("Area", "area"), ("Volume", "volume"),
                       ("Weight", "weight"), ("Count", "count")):
        if word in name:
            return kind
    return "length"


def _merged_quantities(qentries, extra) -> list:
    """The measured Qto entries with the tag's quantities applied: same
    name → the tag's value; a new name is appended."""
    out = list(qentries)
    index = {name: i for i, (_kind, name, _val) in enumerate(out)}
    for name, value in (extra or {}).items():
        value = _number(value)
        if not isinstance(name, str) or not name or value is None:
            continue
        if name in index:
            kind = out[index[name]][0]
            out[index[name]] = (kind, name, value)
        else:
            index[name] = len(out)
            out.append((_quantity_kind(name), name, value))
    return out


def _property_value(value):
    """A typed IFC value for a property, or None when it has no IFC type."""
    if isinstance(value, bool):
        return f"IFCBOOLEAN({'.T.' if value else '.F.'})"
    if isinstance(value, int):
        return f"IFCINTEGER({value})"
    number = _number(value)
    if number is not None:
        return f"IFCREAL({_f(number)})"
    if isinstance(value, str):
        kind = "IFCLABEL" if len(value) <= 255 else "IFCTEXT"
        return f"{kind}({_s(value)})"
    return None


def _write_psets(w: _Writer, elem: int, psets) -> None:
    """One IfcPropertySet per tag pset, each property a single value."""
    if not isinstance(psets, dict):
        return
    for pset_name, props in psets.items():
        if (not isinstance(pset_name, str) or not pset_name
                or pset_name.startswith("Qto_") or not isinstance(props, dict)):
            continue
        values = []
        for prop, value in props.items():
            typed = _property_value(value) if isinstance(prop, str) else None
            if typed is not None:
                values.append(w.add("IFCPROPERTYSINGLEVALUE", _s(prop), "$",
                                    typed, "$"))
        if values:
            pset = w.add("IFCPROPERTYSET", _s(ifc_guid()), "$", _s(pset_name),
                         "$", "(" + ",".join(f"#{v}" for v in values) + ")")
            w.add("IFCRELDEFINESBYPROPERTIES", _s(ifc_guid()), "$", "$", "$",
                  f"(#{elem})", f"#{pset}")


def _apply_tag_attributes(cls: str, attrs: list, tag: dict) -> None:
    """Write the tag's text attributes and enumerations into ``attrs``."""
    texts = tag.get("attributes") if isinstance(tag.get("attributes"), dict) \
        else {}
    slots = dict(_TEXT_SLOTS)
    slots.update(_CLASS_TEXT_SLOTS.get(cls, {}))
    given = {}
    for name, value in texts.items():
        pos = slots.get(name)
        if pos is not None and isinstance(value, str) and value:
            attrs[pos] = _s(value)
            given[name] = value
    enums = tag.get("enums") if isinstance(tag.get("enums"), dict) else {}
    for name, value in enums.items():
        spec = _ENUMS.get((cls, name))
        if spec is None or not isinstance(value, str):
            continue
        pos, allowed = spec
        value = value.strip().upper()
        if value not in allowed:
            continue
        if value == "USERDEFINED" and not given.get(_USERDEFINED_NEEDS.get(name)):
            value = "NOTDEFINED"            # IFC4: USERDEFINED needs its text
        attrs[pos] = f".{value}."


def save_ifc(scene, path, project_name: str = "IngeTrazo project") -> int:
    """Write every tagged BIM object to ``path`` as IFC4. Returns how many
    elements were exported; raises ``ValueError`` when nothing is tagged."""
    objects = collect_objects(scene)
    if not objects:
        raise ValueError("No tagged BIM objects to export")

    w = _Writer()
    origin = w.add("IFCCARTESIANPOINT", "(0.,0.,0.)")
    axis = w.add("IFCAXIS2PLACEMENT3D", f"#{origin}", "$", "$")
    context = w.add("IFCGEOMETRICREPRESENTATIONCONTEXT",
                    "$", "'Model'", "3", "1.E-5", f"#{axis}", "$")
    units = [
        w.add("IFCSIUNIT", "*", ".LENGTHUNIT.", "$", ".METRE."),
        w.add("IFCSIUNIT", "*", ".AREAUNIT.", "$", ".SQUARE_METRE."),
        w.add("IFCSIUNIT", "*", ".VOLUMEUNIT.", "$", ".CUBIC_METRE."),
    ]
    unit_assign = w.add("IFCUNITASSIGNMENT",
                        "(" + ",".join(f"#{u}" for u in units) + ")")
    project = w.add("IFCPROJECT", _s(ifc_guid()), "$", _s(project_name),
                    "$", "$", "$", "$", f"(#{context})", f"#{unit_assign}")
    placement = w.add("IFCLOCALPLACEMENT", "$", f"#{axis}")
    site = w.add("IFCSITE", _s(ifc_guid()), "$", "'Site'", "$", "$",
                 f"#{placement}", "$", "$", ".ELEMENT.",
                 "$", "$", "$", "$", "$")
    building = w.add("IFCBUILDING", _s(ifc_guid()), "$", "'Building'", "$",
                     "$", f"#{placement}", "$", "$", ".ELEMENT.",
                     "$", "$", "$")
    storey = w.add("IFCBUILDINGSTOREY", _s(ifc_guid()), "$", "'Storey'", "$",
                   "$", f"#{placement}", "$", "$", ".ELEMENT.", "0.")
    w.add("IFCRELAGGREGATES", _s(ifc_guid()), "$", "$", "$",
          f"#{project}", f"(#{site})")
    w.add("IFCRELAGGREGATES", _s(ifc_guid()), "$", "$", "$",
          f"#{site}", f"(#{building})")
    w.add("IFCRELAGGREGATES", _s(ifc_guid()), "$", "$", "$",
          f"#{building}", f"(#{storey})")

    element_ids = []
    used_guids: set = set()
    for obj in objects:
        faces = obj.get("faces") or list(obj["group"].mesh.faces)
        if not faces:
            continue
        rep = _brep(w, faces, context)
        pds = w.add("IFCPRODUCTDEFINITIONSHAPE", "$", "$", f"(#{rep})")
        from core.bim import IFC_CLASSES
        cls = obj["class"] if obj["class"] in IFC_CLASSES \
            else "IfcBuildingElementProxy"
        tag = obj.get("tag") or {}
        qset_name, qentries, _metrado = class_quantities(obj["class"], faces)
        qentries = _merged_quantities(qentries, tag.get("quantities"))
        guid = tag.get("guid")
        if not _is_guid(guid) or guid in used_guids:
            guid = ifc_guid()               # absent, malformed or a copy's
        used_guids.add(guid)
        total = _ATTR_COUNT.get(cls, _DEFAULT_ATTRS)
        attrs = [_s(guid), "$", _s(obj["name"] or cls), "$", "$",
                 f"#{placement}", f"#{pds}", "'IngeTrazo'"]
        if cls in ("IfcDoor", "IfcWindow"):
            # Attributes 9/10 are OverallHeight/OverallWidth — viewers and
            # schedules read the leaf dimensions from here.
            dims = {name: val for kind, name, val in qentries
                    if kind == "length"}
            attrs.append(_f(dims["Height"]) if "Height" in dims else "$")
            attrs.append(_f(dims["Width"]) if "Width" in dims else "$")
        if cls == "IfcReinforcingBar":
            # Tag, SteelGrade, then NominalDiameter, CrossSectionArea and
            # BarLength: what a bar schedule reads (#495).
            dims = {name: val for kind, name, val in qentries
                    if kind == "length"}
            d = dims.get("NominalDiameter")
            attrs += ["$",
                      _f(d) if d is not None else "$",
                      _f(math.pi * d * d / 4.0) if d is not None else "$",
                      _f(dims["Length"]) if "Length" in dims else "$",
                      ".NOTDEFINED."]
        attrs += ["$"] * (total - len(attrs))
        _apply_tag_attributes(cls, attrs, tag)
        elem = w.add(cls.upper(), *attrs)
        element_ids.append(elem)
        _write_psets(w, elem, tag.get("psets"))

        # Per-class BaseQuantities (Qto_* naming): the takeoff numbers in
        # the budget's own measures, straight in the file.
        quantities = [
            f"#{w.add(_Q_ENTITY[kind], _s(name), '$', '$', _f(val), '$')}"
            for kind, name, val in qentries
        ]
        if quantities:
            eq = w.add("IFCELEMENTQUANTITY", _s(ifc_guid()), "$",
                       _s(qset_name), "$", "$",
                       "(" + ",".join(quantities) + ")")
            w.add("IFCRELDEFINESBYPROPERTIES", _s(ifc_guid()), "$", "$", "$",
                  f"(#{elem})", f"#{eq}")

    if not element_ids:
        raise ValueError("No tagged BIM objects to export")
    w.add("IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(ifc_guid()), "$", "$", "$",
          "(" + ",".join(f"#{e}" for e in element_ids) + ")", f"#{storey}")

    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    name = Path(path).name.replace("'", "''")
    header = (
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_DESCRIPTION((''),'2;1');\n"
        f"FILE_NAME('{name}','{stamp}',('IngeTrazo'),('IngeTrazo'),"
        "'IngeTrazo','IngeTrazo','');\n"
        "FILE_SCHEMA(('IFC4'));\n"
        "ENDSEC;\n"
        "DATA;\n"
    )
    footer = "ENDSEC;\nEND-ISO-10303-21;\n"
    Path(path).write_text(header + "\n".join(w.lines) + "\n" + footer,
                          encoding="ascii")
    return len(element_ids)
