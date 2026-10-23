"""Turn the plugin's mesh dump ("DTM1") into the binary glTF we store.

Plugin 6.1.15 stopped writing the glTF itself: it was pure serialisation and
cost the plugin ~3.5k Plugin Hub tokens. It now sends the mesh it built
(``io.droptracker.modelexport.GlbExporter``) and this writes the same file the
plugin's ``GlbWriter`` used to: one mesh, one shared vertex buffer, one
primitive per texture/translucency run, unlit materials, textures embedded as
PNG. Older plugins still upload a ready ``.glb``; both end up in
``services.player_model``'s validator before anything is stored.

The dump is attacker-controlled, so every count is checked against the bytes
actually present and against generous ceilings before anything is allocated.
Stdlib only.
"""
from __future__ import annotations

import json
import struct
from typing import List, Optional

MAGIC = 0x44544D31  # "DTM1"
MAX_VERTICES = 300_000
MAX_INDICES = 1_500_000
MAX_PRIMITIVES = 512
MAX_PNG_BYTES = 2 * 1024 * 1024

_GLB_MAGIC = 0x46546C67
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942
_ARRAY_BUFFER = 34962
_ELEMENT_ARRAY_BUFFER = 34963
_UBYTE, _USHORT, _UINT, _FLOAT = 5121, 5123, 5125, 5126
_MAX_SHORT_INDEX = 65535
_UNITS_PER_METRE = 128.0


class MeshError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise MeshError("truncated mesh")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def int(self) -> int:
        return struct.unpack(">i", self.take(4))[0]

    def byte(self) -> int:
        return self.take(1)[0]

    def float(self) -> float:
        return struct.unpack(">f", self.take(4))[0]

    def floats(self, limit: int) -> Optional[bytes]:
        """A length-prefixed float array as little-endian float32 bytes, or None."""
        n = self.int()
        if n == -1:
            return None
        if n < 0 or n > limit:
            raise MeshError("bad float count")
        raw = self.take(4 * n)
        return struct.pack(f"<{n}f", *struct.unpack(f">{n}f", raw)) if n else b""

    def utf(self) -> str:
        n = struct.unpack(">H", self.take(2))[0]
        return self.take(n).decode("utf-8", "replace")


def parse(data: bytes) -> dict:
    """The dump's contents, checked for consistency. Raises MeshError."""
    r = _Reader(data)
    if len(data) < 4 or r.int() != MAGIC:
        raise MeshError("not a mesh dump")
    name = r.utf()[:32] or "model"
    face_count = r.int()
    positions = r.floats(3 * MAX_VERTICES)
    if not positions or len(positions) % 12:
        raise MeshError("bad positions")
    vertices = len(positions) // 12
    n_colors = r.int()
    if n_colors != 4 * vertices:
        raise MeshError("bad colors")
    colors = r.take(n_colors)
    uvs = r.floats(2 * MAX_VERTICES)
    priorities = r.floats(MAX_VERTICES)
    if uvs is not None and len(uvs) != 8 * vertices:
        raise MeshError("bad uvs")
    if priorities is not None and len(priorities) != 4 * vertices:
        raise MeshError("bad priorities")
    n_indices = r.int()
    if n_indices <= 0 or n_indices > MAX_INDICES:
        raise MeshError("bad index count")
    indices = struct.unpack(f">{n_indices}i", r.take(4 * n_indices))
    if min(indices) < 0 or max(indices) >= vertices:
        raise MeshError("index out of range")
    bounds = [r.floats(3), r.floats(3)]
    if any(b is None or len(b) != 12 for b in bounds):
        raise MeshError("bad bounds")
    n_prims = r.int()
    if n_prims <= 0 or n_prims > MAX_PRIMITIVES:
        raise MeshError("bad primitive count")
    primitives, textures = [], {}
    for _ in range(n_prims):
        texture_id, offset, count = r.int(), r.int(), r.int()
        translucent = r.byte() != 0
        state = r.byte()
        if offset < 0 or count <= 0 or offset + count > n_indices:
            raise MeshError("primitive outside the index buffer")
        if state == 1:
            average = r.int()
            scroll_u, scroll_v = r.float(), r.float()
            png_len = r.int()
            if png_len > MAX_PNG_BYTES or png_len < -1:
                raise MeshError("texture too large")
            png = r.take(png_len) if png_len >= 0 else None
            textures[texture_id] = {"id": texture_id, "png": png, "average": average,
                                    "scroll_u": scroll_u, "scroll_v": scroll_v}
        elif state == 2:
            if texture_id not in textures:
                raise MeshError("texture sent before its data")
        elif state != 0:
            raise MeshError("bad texture state")
        primitives.append({"texture_id": texture_id, "offset": offset, "count": count,
                           "translucent": translucent, "texture": textures.get(texture_id) if state else None})
    if r.pos != len(data):
        raise MeshError("trailing bytes")
    return {"name": name, "face_count": face_count, "vertices": vertices, "positions": positions,
            "colors": colors, "uvs": uvs, "priorities": priorities, "indices": indices,
            "min": struct.unpack("<3f", bounds[0]), "max": struct.unpack("<3f", bounds[1]),
            "primitives": primitives}


class _F32(float):
    """A float written the way Java writes a float: its shortest float32 form."""

    def __repr__(self):
        for digits in range(1, 10):
            text = f"{self:.{digits}g}"
            if struct.unpack("<f", struct.pack("<f", float(text)))[0] == struct.unpack("<f", struct.pack("<f", self))[0]:
                break
        if "e" in text or "E" in text:
            text = repr(float(text))
        return text if "." in text or "e" in text or "inf" in text or "nan" in text else text + ".0"


def _f(*values) -> List[float]:
    return [_F32(struct.unpack("<f", struct.pack("<f", v))[0]) for v in values]


def _plain_dumps(obj) -> str:
    """Compact JSON in insertion order, floats as Java writes floats."""
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _plain_dumps(v) for k, v in obj.items()) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(_plain_dumps(v) for v in obj) + "]"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, _F32):
        return repr(obj)
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return repr(_F32(obj))
    return json.dumps(obj, ensure_ascii=False)


def to_glb(mesh: dict) -> bytes:
    """The binary glTF for a parsed mesh, as GlbWriter wrote it."""
    bin_ = bytearray()
    buffer_views, accessors, materials, textures, images = [], [], [], [], []
    texture_index_by_id = {}

    def view(data: bytes, target: int) -> int:
        bin_.extend(b"\0" * ((4 - len(bin_) % 4) % 4))
        entry = {"buffer": 0, "byteOffset": len(bin_), "byteLength": len(data)}
        if target > 0:
            entry["target"] = target
        bin_.extend(data)
        buffer_views.append(entry)
        return len(buffer_views) - 1

    def accessor(data, component, kind, count, lo=None, hi=None, normalized=False) -> int:
        entry = {"bufferView": view(data, _ARRAY_BUFFER), "componentType": component, "count": count, "type": kind}
        if normalized:
            entry["normalized"] = True
        if lo is not None:
            entry["min"] = _f(*lo)
        if hi is not None:
            entry["max"] = _f(*hi)
        accessors.append(entry)
        return len(accessors) - 1

    n = mesh["vertices"]
    position = accessor(mesh["positions"], _FLOAT, "VEC3", n, mesh["min"], mesh["max"])
    color = accessor(mesh["colors"], _UBYTE, "VEC4", n, normalized=True)
    uv = -1 if mesh["uvs"] is None else accessor(mesh["uvs"], _FLOAT, "VEC2", n)
    priority = -1 if mesh["priorities"] is None else accessor(mesh["priorities"], _FLOAT, "SCALAR", n)

    wide = n > _MAX_SHORT_INDEX
    stride = 4 if wide else 2
    index_view = view(struct.pack(f"<{len(mesh['indices'])}{'I' if wide else 'H'}", *mesh["indices"]),
                      _ELEMENT_ARRAY_BUFFER)

    primitives = []
    for p in mesh["primitives"]:
        tex = p["texture"]
        accessors.append({"bufferView": index_view, "byteOffset": p["offset"] * stride,
                          "componentType": _UINT if wide else _USHORT, "count": p["count"], "type": "SCALAR"})
        has_image = tex is not None and tex["png"] is not None
        attributes = {"POSITION": position, "COLOR_0": color}
        if has_image and uv != -1:
            attributes["TEXCOORD_0"] = uv
        if priority != -1:
            attributes["_PRIORITY"] = priority
        primitives.append({"attributes": attributes, "indices": len(accessors) - 1, "material": len(materials)})

        texture_index = -1
        if has_image:
            if tex["id"] not in texture_index_by_id:
                textures.append({"sampler": 0, "source": len(images)})
                images.append({"name": f"texture-{tex['id']}", "bufferView": view(tex["png"], -1),
                               "mimeType": "image/png"})
                texture_index_by_id[tex["id"]] = len(textures) - 1
            texture_index = texture_index_by_id[tex["id"]]

        if has_image:
            pbr = {"baseColorFactor": _f(1, 1, 1, 1), "baseColorTexture": {"index": texture_index}}
        elif tex is not None:
            avg = tex["average"]
            pbr = {"baseColorFactor": _f(((avg >> 16) & 0xFF) / 255, ((avg >> 8) & 0xFF) / 255,
                                         (avg & 0xFF) / 255, 1)}
        else:
            pbr = {"baseColorFactor": _f(1, 1, 1, 1)}
        pbr["metallicFactor"], pbr["roughnessFactor"] = _f(0)[0], _f(1)[0]
        material = {"name": "untextured" if tex is None else f"texture-{tex['id']}",
                    "pbrMetallicRoughness": pbr, "extensions": {"KHR_materials_unlit": {}}, "doubleSided": True}
        if p["translucent"]:
            material["alphaMode"] = "BLEND"
        elif has_image:
            material["alphaMode"], material["alphaCutoff"] = "MASK", _f(0.5)[0]
        else:
            material["alphaMode"] = "OPAQUE"
        if has_image and (tex["scroll_u"] != 0 or tex["scroll_v"] != 0):
            material["extras"] = {"runeprofile": {"textureId": tex["id"], "scrollU": _f(tex["scroll_u"])[0],
                                                  "scrollV": _f(tex["scroll_v"])[0]}}
        materials.append(material)

    scale = 1 / _UNITS_PER_METRE
    gltf = {
        "asset": {"version": "2.0", "generator": "RuneProfile",
                  "extras": {"source": "droptracker-plugin", "faceCount": mesh["face_count"], "vertexCount": n}},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": mesh["name"], "scale": _f(scale, scale, scale)}],
        "meshes": [{"primitives": primitives, "name": mesh["name"]}],
        "materials": materials,
    }
    if textures:
        gltf["textures"], gltf["images"] = textures, images
        gltf["samplers"] = [{"magFilter": 9728, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
    gltf["accessors"] = accessors
    gltf["bufferViews"] = buffer_views
    gltf["buffers"] = [{"byteLength": len(bin_)}]
    gltf["extensionsUsed"] = ["KHR_materials_unlit"]

    js = _plain_dumps(gltf).encode("utf-8")
    js += b" " * ((4 - len(js) % 4) % 4)
    body = bytes(bin_) + b"\0" * ((4 - len(bin_) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(body)
    return (struct.pack("<III", _GLB_MAGIC, 2, total) + struct.pack("<II", len(js), _CHUNK_JSON) + js
            + struct.pack("<II", len(body), _CHUNK_BIN) + body)


def convert(data: bytes) -> bytes:
    """A mesh dump straight to glTF bytes. Raises MeshError."""
    return to_glb(parse(data))
