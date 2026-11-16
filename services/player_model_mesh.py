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

Plugin 6.1.28 stopped building the mesh too ("DTM2", ``ModelDump``): it sends
the game model's arrays as the client holds them, plus the pixels of each
texture the faces name, and ``parse_raw`` does what the plugin's
``ModelMeshBuilder``, ``GameTextures`` and ``JagexColor`` did. It arrives at the
same mesh, value for value, so the stored file does not depend on which plugin
sent it. That half needs numpy, imported only when such a dump arrives.

The mesh building is a port of the plugin's code, which was adapted from
RuneProfile (github.com/ReinhardtR/runeprofile-plugin, Copyright (c) 2022,
Reinhardt Rijna, BSD 2-Clause) and, for the UV projection, RuneLite's GPU plugin.
"""
from __future__ import annotations

import json
import struct
import zlib
from typing import List, Optional

MAGIC = 0x44544D31  # "DTM1"
MAX_VERTICES = 300_000
MAX_INDICES = 1_500_000
MAX_PRIMITIVES = 512
MAX_PNG_BYTES = 2 * 1024 * 1024

RAW_MAGIC = 0x44544D32  # "DTM2", inside gzip
MAX_RAW_BYTES = 48 * 1024 * 1024
MAX_FACES = MAX_INDICES // 3
MAX_TEXTURE_TRIANGLES = 65_536
MAX_TEXTURES = 64
MAX_TEXTURE_PIXELS = 512 * 512
_NO_TEXTURE = -1
_FLAT_SHADED = -1  # third face colour: one colour for the whole triangle
_HIDDEN_FACE = -2  # third face colour: a face the game never draws
_FLOAT_MAX = 3.4028234663852886e38

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


def _inflate(data: bytes) -> bytes:
    """Gunzip with a ceiling, so a small upload cannot expand without bound."""
    try:
        inflater = zlib.decompressobj(wbits=31)
        raw = inflater.decompress(data, MAX_RAW_BYTES + 1)
    except zlib.error:
        raise MeshError("bad compression")
    if len(raw) > MAX_RAW_BYTES or not inflater.eof or inflater.unused_data:
        raise MeshError("bad compression")
    return raw


_PALETTE = None
_GREYS = None


def _palette():
    """Packed HSL to RGB, as the plugin's JagexColor.createPalette(0.9) built it."""
    global _PALETTE
    if _PALETTE is None:
        import numpy as np

        hsl = np.arange(65536, dtype=np.int64)
        hue = ((hsl >> 10) & 63) / 64.0 + 0.5 / 64.0
        saturation = ((hsl >> 7) & 7) / 8.0 + 0.5 / 8.0
        luminance = (hsl & 127) / 128.0
        chroma = (1.0 - np.abs(2.0 * luminance - 1.0)) * saturation
        x = chroma * (1 - np.abs(np.fmod(hue * 6.0, 2.0) - 1.0))
        lightness = luminance - chroma / 2
        sector = (hue * 6.0).astype(np.int64)
        r, g, b = lightness.copy(), lightness.copy(), lightness.copy()
        for which, strong, weak in ((0, r, g), (1, g, r), (2, g, b), (3, b, g), (4, b, r), (5, r, b)):
            mask = sector == which if which < 5 else sector >= 5
            strong[mask] += chroma[mask]
            weak[mask] += x[mask]

        def pack(red, green, blue):
            return ((red * 256.0).astype(np.int64) << 16) | ((green * 256.0).astype(np.int64) << 8) \
                | (blue * 256.0).astype(np.int64)

        rgb = pack(r, g, b)
        rgb = pack(np.power((rgb >> 16) / 256.0, 0.9), np.power(((rgb >> 8) & 255) / 256.0, 0.9),
                   np.power((rgb & 255) / 256.0, 0.9))
        rgb[rgb == 0] = 1
        _PALETTE = rgb.astype(np.int32)
    return _PALETTE


def _greys():
    """A textured face's 7 bit lightness as the grey its texels are multiplied by."""
    global _GREYS
    if _GREYS is None:
        import numpy as np

        level = np.arange(128, dtype=np.float32) / np.float32(127) * np.float32(255)
        level = np.floor(level.astype(np.float64) + 0.5).astype(np.int32)
        _GREYS = (level << 16) | (level << 8) | level
    return _GREYS


def _png(rgba: bytes, size: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    stride = 4 * size
    rows = b"".join(b"\0" + rgba[y * stride:(y + 1) * stride] for y in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def _texture(texture_id: int, direction: int, speed: int, pixels) -> Optional[dict]:
    """What GameTextures made of one texture: PNG, mean colour, scroll per second."""
    import numpy as np

    count = 0 if pixels is None else len(pixels)
    size = int(round(count ** 0.5))
    if count == 0 or size * size != count:
        return None
    # A texel of 0 is the game's transparent one; the renderer discards it.
    drawn = pixels != 0
    rgba = np.zeros((count, 4), dtype=np.uint8)
    rgba[:, 0], rgba[:, 1], rgba[:, 2] = (pixels >> 16) & 0xFF, (pixels >> 8) & 0xFF, pixels & 0xFF
    rgba[:, 3] = 255
    rgba[~drawn] = 0
    if drawn.any():
        mean = [int(rgba[drawn, k].astype(np.int64).sum()) // int(drawn.sum()) for k in range(3)]
        average = mean[0] << 16 | mean[1] << 8 | mean[2]
    else:
        average = 0xFFFFFF
    # The client shifts UVs by speed/128 each 20ms client tick.
    axis = {1: (0.0, -1.0), 2: (-1.0, 0.0), 3: (0.0, 1.0), 4: (1.0, 0.0)}.get(direction)
    scroll = (0.0, 0.0)
    if axis is not None:
        per_second = np.float32(speed) * np.float32(1 / 128) * np.float32(50)
        scroll = tuple(float(np.float32(a) * per_second) for a in axis)
    return {"id": texture_id, "png": _png(rgba.tobytes(), size), "average": average,
            "scroll_u": scroll[0], "scroll_v": scroll[1]}


def parse_raw(data: bytes, name: str = "model") -> dict:
    """A DTM2 dump as the same mesh ``parse`` returns. Raises MeshError.

    Mirrors the plugin's ModelMeshBuilder exactly, float32 arithmetic included:
    vertices merge only when every value is bit-identical, so a different
    rounding would change the vertex count, not just the last digit.
    """
    import numpy as np

    r = _Reader(_inflate(data))
    if r.int() != RAW_MAGIC:
        raise MeshError("not a model dump")
    n_vertices, n_faces = r.int(), r.int()
    if not 0 < n_vertices <= MAX_VERTICES or not 0 < n_faces <= MAX_FACES:
        raise MeshError("bad model size")

    def ints(expected: Optional[int], limit: int = 0):
        n = r.int()
        if n == -1:
            return None
        if n < 0 or (n != expected if expected is not None else n > limit):
            raise MeshError("bad array length")
        return np.frombuffer(r.take(4 * n), dtype=">i4").astype(np.int32)

    def face_bytes():
        n = r.int()
        if n == -1:
            return None
        if n != n_faces:
            raise MeshError("bad array length")
        return np.frombuffer(r.take(n), dtype=np.uint8).astype(np.int32)

    vx, vy, vz = (np.frombuffer(r.take(4 * n_vertices), dtype=">f4").astype(np.float32) for _ in range(3))
    corners = [ints(n_faces) for _ in range(3)]
    colors = [ints(n_faces) for _ in range(3)]
    if any(a is None for a in corners + colors):
        raise MeshError("missing face data")
    transparencies, priorities, texture_faces = face_bytes(), face_bytes(), face_bytes()
    tex_corners = [ints(None, MAX_TEXTURE_TRIANGLES) for _ in range(3)]
    n = r.int()
    if n not in (-1, n_faces):
        raise MeshError("bad array length")
    face_textures = (np.full(n_faces, _NO_TEXTURE, dtype=np.int32) if n == -1
                     else np.frombuffer(r.take(2 * n), dtype=">i2").astype(np.int32))
    n_textures = r.int()
    if not 0 <= n_textures <= MAX_TEXTURES:
        raise MeshError("bad texture count")
    textures = {}
    for _ in range(n_textures):
        texture_id, direction, speed = r.int(), r.int(), r.int()
        textures[texture_id] = _texture(texture_id, direction, speed, ints(None, MAX_TEXTURE_PIXELS))
    if r.pos != len(r.data):
        raise MeshError("trailing bytes")

    # Which faces are drawn, bucketed one primitive per (texture, translucent):
    # untextured before textured, opaque before translucent, faces in order.
    alpha = 255 - transparencies if transparencies is not None else np.full(n_faces, 255, dtype=np.int32)
    drawn = np.nonzero((colors[2] != _HIDDEN_FACE) & (alpha != 0))[0]
    if not len(drawn):
        raise MeshError("empty model")
    translucent = alpha != 255
    order = drawn[np.lexsort((drawn, translucent[drawn], face_textures[drawn]))]
    tex_of, translucent_of = face_textures[order], translucent[order]
    textured = tex_of != _NO_TEXTURE
    idx = np.stack([c[order] for c in corners], axis=1)
    if idx.min() < 0 or idx.max() >= n_vertices:
        raise MeshError("face outside the vertex buffer")

    flat = colors[2][order] == _FLAT_SHADED
    first = colors[0][order]
    corner_colors = np.stack([first, np.where(flat, first, colors[1][order]),
                              np.where(flat, first, colors[2][order])], axis=1)
    rgb = np.where(textured[:, None], _greys()[np.clip(corner_colors, 0, 127)],
                   _palette()[corner_colors & 0xFFFF])

    u = np.zeros(idx.shape, dtype=np.float32)
    v = np.zeros(idx.shape, dtype=np.float32)
    if textured.any():
        # The default mapping: the triangle straight onto the texture's corner.
        u[textured] = (0.0, 1.0, 0.0)
        v[textured] = (0.0, 0.0, 1.0)
        mapped = textured & (texture_faces[order] != 255) if texture_faces is not None else None
        if mapped is not None and mapped.any():
            if any(a is None for a in tex_corners):
                raise MeshError("missing texture triangles")
            triangle = texture_faces[order][mapped]
            if triangle.max() >= min(len(a) for a in tex_corners):
                raise MeshError("texture triangle out of range")
            tex = [a[triangle] for a in tex_corners]
            if min(t.min() for t in tex) < 0 or max(t.max() for t in tex) >= n_vertices:
                raise MeshError("texture triangle outside the vertex buffer")
            u[mapped], v[mapped] = _face_uvs((vx, vy, vz), idx[mapped], tex)

    # Y up: negate Y and Z (a half turn about X, so winding carries over).
    px, py, pz = vx[idx], -vy[idx], -vz[idx]
    record = np.zeros(idx.size, dtype=[("p", "<f4", 3), ("uv", "<f4", 2), ("rgb", "<i4"),
                                       ("alpha", "<i4"), ("priority", "<i4")])
    record["p"] = np.stack([px.ravel(), py.ravel(), pz.ravel()], axis=1)
    record["uv"] = np.stack([u.ravel(), v.ravel()], axis=1)
    for field in ("p", "uv"):  # Java compares floats by floatToIntBits: one NaN
        bits = record[field].view("<u4")
        bits[np.isnan(record[field])] = 0x7FC00000
    record["rgb"] = rgb.ravel()
    record["alpha"] = np.repeat(alpha[order], 3)
    if priorities is not None:
        record["priority"] = np.repeat(priorities[order], 3)

    # Merge identical corners, numbering vertices in the order first seen.
    _, first_seen, inverse = np.unique(record.view(f"V{record.dtype.itemsize}"), return_index=True,
                                       return_inverse=True)
    by_appearance = np.argsort(first_seen, kind="stable")
    slot = np.empty(len(first_seen), dtype=np.int64)
    slot[by_appearance] = np.arange(len(first_seen))
    indices = slot[inverse.ravel()]
    vertices = record[first_seen[by_appearance]]

    positions = np.ascontiguousarray(vertices["p"])
    rgba = np.empty((len(vertices), 4), dtype=np.uint8)
    rgba[:, 0], rgba[:, 1], rgba[:, 2] = (vertices["rgb"] >> 16) & 0xFF, (vertices["rgb"] >> 8) & 0xFF, \
        vertices["rgb"] & 0xFF
    rgba[:, 3] = vertices["alpha"] & 0xFF
    with np.errstate(all="ignore"):
        lo = [float(np.fmin.reduce(positions[:, k], initial=_FLOAT_MAX)) for k in range(3)]
        hi = [float(np.fmax.reduce(positions[:, k], initial=-_FLOAT_MAX)) for k in range(3)]

    primitives = []
    starts = np.nonzero(np.r_[True, (tex_of[1:] != tex_of[:-1]) | (translucent_of[1:] != translucent_of[:-1])])[0]
    for start, end in zip(starts, np.r_[starts[1:], len(order)]):
        texture_id = int(tex_of[start])
        primitives.append({"texture_id": texture_id, "offset": int(start) * 3, "count": int(end - start) * 3,
                           "translucent": bool(translucent_of[start]),
                           "texture": textures.get(texture_id) if texture_id != _NO_TEXTURE else None})
    return {"name": name, "face_count": n_faces, "vertices": len(vertices), "positions": positions.tobytes(),
            "colors": rgba.tobytes(),
            "uvs": np.ascontiguousarray(vertices["uv"]).tobytes() if textured.any() else None,
            "priorities": vertices["priority"].astype("<f4").tobytes() if priorities is not None else None,
            "indices": indices.tolist(), "min": tuple(lo), "max": tuple(hi), "primitives": primitives}


def _face_uvs(xyz, corners, tex):
    """Projects each face's corners onto its texture triangle, as the client's GPU plugin does.

    The texture triangle is three model vertices: the texture's origin and the
    ends of its U and V axes. Every step is float32 and in the plugin's order.
    """
    import numpy as np

    with np.errstate(all="ignore"):
        origin = [axis[tex[0]] for axis in xyz]
        axis_u = [axis[tex[1]] - o for axis, o in zip(xyz, origin)]
        axis_v = [axis[tex[2]] - o for axis, o in zip(xyz, origin)]
        # The face's own corners, relative to the same origin: rel[corner][axis].
        rel = [[axis[corners[:, k]] - o for axis, o in zip(xyz, origin)] for k in range(3)]

        def cross(a, b):
            return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]

        def dot(a, b):
            return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

        normal = cross(axis_u, axis_v)
        out = []
        # Projecting along axisV x normal isolates U; the reciprocal dot with
        # axisU lands its end on u = 1. Then the same with the axes swapped.
        for along, other in ((axis_v, axis_u), (axis_u, axis_v)):
            proj = cross(along, normal)
            scale = np.float32(1.0) / dot(proj, other)
            out.append(np.stack([dot(proj, rel[k]) * scale for k in range(3)], axis=1))
    return out[0], out[1]


def convert(data: bytes, name: str = "model") -> bytes:
    """An uploaded mesh in either form straight to glTF bytes. Raises MeshError.

    ``name`` labels a DTM2 dump, which does not carry one ("player" or "pet").
    """
    if data[:2] != b"\x1f\x8b":
        return to_glb(parse(data))
    try:
        return to_glb(parse_raw(data, name))
    except MeshError:
        raise
    except (ValueError, IndexError, OverflowError, struct.error) as exc:
        # Whatever a hostile dump trips in numpy is still just a bad dump.
        raise MeshError(f"bad model dump: {exc}")
