"""services/player_model_mesh.py, the DTM2 half: the game model to a mesh.

Plugin 6.1.28 sends the model's arrays as the client holds them and the server
does what the plugin's ModelMeshBuilder, GameTextures and JagexColor did. What
these pin:

* the mesh is value-for-value the one the plugin built from the same model
  (fixtures: one random model written by both plugin exporters; 400 such
  models matched when this was built, as did all 65,536 palette entries);
* which faces are drawn, and how they are bucketed into primitives;
* a dump that lies about its sizes, points outside its arrays or expands
  without bound is refused, and nothing it trips escapes as another error;
* the upload route takes the new form in the same fields as the old one.
"""
from __future__ import annotations

import gzip
import importlib.util
import io
import json
import os
import struct
import sys
import zlib

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FIXTURES = os.path.join(_ROOT, "tests", "fixtures", "player_model_mesh")


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pm = _load("_real_player_model_mesh_raw", "services", "player_model_mesh.py")


def _fixture(name):
    with open(os.path.join(_FIXTURES, name), "rb") as fh:
        return fh.read()


def _ints(values, fmt="i"):
    return struct.pack(">i", -1) if values is None else struct.pack(f">i{len(values)}{fmt}", len(values), *values)


def _dump(vertices, faces, colors, transparencies=None, priorities=None, texture_faces=None, tex=None,
          face_textures=None, textures=(), extra=b"", counts=None, compress=True):
    """A DTM2 dump the way ModelDump writes one. ``faces`` is a list of corner triples."""
    n_vertices, n_faces = counts or (len(vertices), len(faces))
    raw = struct.pack(">iii", pm.RAW_MAGIC, n_vertices, n_faces)
    for axis in range(3):
        raw += struct.pack(f">{len(vertices)}f", *[v[axis] for v in vertices])
    for corner in range(3):
        raw += _ints([f[corner] for f in faces])
    for corner in range(3):
        raw += _ints([c[corner] for c in colors])
    for per_face in (transparencies, priorities, texture_faces):
        raw += _ints(per_face, "B")
    for corner in range(3):
        raw += _ints(None if tex is None else [t[corner] for t in tex])
    raw += _ints(face_textures, "h")
    raw += struct.pack(">i", len(textures))
    for texture_id, direction, speed, pixels in textures:
        raw += struct.pack(">iii", texture_id, direction, speed) + _ints(pixels)
    raw += extra
    return gzip.compress(raw) if compress else raw


_TRIANGLE = [(0.0, 0.0, 0.0), (128.0, 0.0, 0.0), (0.0, 128.0, 0.0)]
_FLAT = (1000, 2000, -1)


def _gltf(glb):
    length = struct.unpack("<I", glb[12:16])[0]
    return json.loads(glb[20:20 + length])


def _png_pixels(png):
    from PIL import Image

    return Image.open(io.BytesIO(png)).convert("RGBA").tobytes()


# --------------------------------------------------------------------------- #
# Against the plugin's own mesh builder
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", range(5))
def test_the_mesh_is_the_one_the_plugin_built(case):
    built = pm.parse(_fixture(f"raw_{case}.dtm1"))
    ours = pm.parse_raw(_fixture(f"raw_{case}.dtm2"), built["name"])
    for key in ("name", "face_count", "vertices", "positions", "colors", "uvs", "priorities"):
        assert ours[key] == built[key], key
    assert list(ours["indices"]) == list(built["indices"])
    for key in ("min", "max"):
        assert struct.pack("<3f", *ours[key]) == struct.pack("<3f", *built[key]), key
    assert len(ours["primitives"]) == len(built["primitives"])
    for mine, theirs in zip(ours["primitives"], built["primitives"]):
        for key in ("texture_id", "offset", "count", "translucent"):
            assert mine[key] == theirs[key], key
        assert (mine["texture"] is None) == (theirs["texture"] is None)
        if theirs["texture"] is not None:
            for key in ("id", "average"):
                assert mine["texture"][key] == theirs["texture"][key], key
            for key in ("scroll_u", "scroll_v"):
                assert struct.pack("<f", mine["texture"][key]) == struct.pack("<f", theirs["texture"][key]), key
            # Two PNG encoders: the files differ, the pixels must not.
            assert _png_pixels(mine["texture"]["png"]) == _png_pixels(theirs["texture"]["png"])


def test_the_fixtures_cover_textures_and_translucency():
    seen = [p for case in range(5) for p in pm.parse(_fixture(f"raw_{case}.dtm1"))["primitives"]]
    assert any(p["texture"] is not None for p in seen)
    assert any(p["texture_id"] != -1 and p["texture"] is None for p in seen)
    assert any(p["translucent"] for p in seen)


# --------------------------------------------------------------------------- #
# What is drawn
# --------------------------------------------------------------------------- #
def test_a_flat_face_takes_its_first_colour_and_the_model_turns_y_up():
    mesh = pm.parse_raw(_dump(_TRIANGLE, [(0, 1, 2)], [_FLAT]), "player")
    assert (mesh["name"], mesh["face_count"], mesh["vertices"]) == ("player", 1, 3)
    palette = int(pm._palette()[1000])
    rgba = bytes([(palette >> 16) & 255, (palette >> 8) & 255, palette & 255, 255])
    assert mesh["colors"] == rgba * 3
    assert mesh["uvs"] is None and mesh["priorities"] is None
    # Y and Z are negated, which leaves negative zeros where they were zero.
    assert struct.unpack("<9f", mesh["positions"]) == (0.0, -0.0, -0.0, 128.0, -0.0, -0.0, 0.0, -128.0, -0.0)
    assert mesh["min"] == (0.0, -128.0, 0.0) and mesh["max"] == (128.0, 0.0, 0.0)
    assert [(p["texture_id"], p["offset"], p["count"], p["translucent"]) for p in mesh["primitives"]] \
        == [(-1, 0, 3, False)]


def test_hidden_and_invisible_faces_are_left_out():
    faces = [(0, 1, 2)] * 3
    colors = [_FLAT, (1000, 2000, -2), _FLAT]
    mesh = pm.parse_raw(_dump(_TRIANGLE, faces, colors, transparencies=[0, 0, 255]))
    assert mesh["face_count"] == 3 and len(mesh["indices"]) == 3
    with pytest.raises(pm.MeshError):
        pm.parse_raw(_dump(_TRIANGLE, [(0, 1, 2)], [(1, 2, -2)]))


def test_primitives_run_untextured_first_and_opaque_before_translucent():
    faces = [(0, 1, 2)] * 4
    pixels = [0x102030, 0, 0x304050, 0x506070]
    mesh = pm.parse_raw(_dump(_TRIANGLE, faces, [(100, 100, 100)] * 4, transparencies=[0, 100, 100, 0],
                              priorities=[5, 5, 5, 5], face_textures=[7, -1, 7, -1],
                              textures=[(7, 3, 2, pixels)]))
    assert [(p["texture_id"], p["translucent"], p["count"]) for p in mesh["primitives"]] \
        == [(-1, False, 3), (-1, True, 3), (7, False, 3), (7, True, 3)]
    texture = mesh["primitives"][2]["texture"]
    assert texture is mesh["primitives"][3]["texture"]
    assert texture["average"] == 0x304050  # the transparent texel is not counted
    assert (texture["scroll_u"], texture["scroll_v"]) == (0.0, 2 * 50 / 128)
    assert _png_pixels(texture["png"]) == bytes([0x10, 0x20, 0x30, 255, 0, 0, 0, 0,
                                                 0x30, 0x40, 0x50, 255, 0x50, 0x60, 0x70, 255])
    # No texture triangle: the face maps straight onto the texture's corner.
    assert struct.unpack("<2f", mesh["uvs"][:8]) == (0.0, 0.0)
    assert struct.unpack(f"<{mesh['vertices']}f", mesh["priorities"]) == (5.0,) * mesh["vertices"]
    gltf = _gltf(pm.to_glb(mesh))
    assert [m["alphaMode"] for m in gltf["materials"]] == ["OPAQUE", "BLEND", "MASK", "BLEND"]
    assert len(gltf["images"]) == 1


def test_a_texture_triangle_places_its_corners_at_the_ends_of_the_axes():
    mesh = pm.parse_raw(_dump(_TRIANGLE, [(0, 1, 2)], [(64, 64, 64)], texture_faces=[0], tex=[(0, 1, 2)],
                              face_textures=[3], textures=[(3, 0, 0, [1, 2, 3, 4])]))
    assert struct.unpack("<6f", mesh["uvs"]) == (0.0, 0.0, 1.0, 0.0, 0.0, 1.0)
    # Lightness 64 of 127, as a grey the texel is multiplied by.
    assert mesh["colors"][:4] == bytes([129, 129, 129, 255])


def test_a_texture_the_client_had_not_loaded_is_drawn_flat():
    mesh = pm.parse_raw(_dump(_TRIANGLE, [(0, 1, 2)], [(64, 64, 64)], face_textures=[3],
                              textures=[(3, 1, 4, None)]))
    assert mesh["primitives"][0]["texture"] is None
    assert _gltf(pm.to_glb(mesh))["materials"][0]["name"] == "untextured"


# --------------------------------------------------------------------------- #
# Hostile input
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dump", [
    _dump(_TRIANGLE, [(0, 1, 3)], [_FLAT]),                                   # corner past the vertices
    _dump(_TRIANGLE, [(0, 1, -1)], [_FLAT]),
    _dump(_TRIANGLE, [(0, 1, 2)], [_FLAT], extra=b"\0"),                      # trailing bytes
    _dump(_TRIANGLE, [(0, 1, 2)], [_FLAT], counts=(3, 2)),                    # more faces than sent
    _dump(_TRIANGLE, [(0, 1, 2)], [_FLAT], counts=(pm.MAX_VERTICES + 1, 1)),
    _dump(_TRIANGLE, [(0, 1, 2)], [_FLAT], counts=(3, pm.MAX_FACES + 1)),
    _dump(_TRIANGLE, [(0, 1, 2)], [_FLAT], counts=(0, 1)),
    _dump(_TRIANGLE, [(0, 1, 2)], [_FLAT], transparencies=[0, 0]),            # wrong per-face length
    _dump(_TRIANGLE, [(0, 1, 2)], [(1, 1, 1)], texture_faces=[4], tex=[(0, 1, 2)], face_textures=[3]),
    _dump(_TRIANGLE, [(0, 1, 2)], [(1, 1, 1)], texture_faces=[0], tex=[(0, 1, 9)], face_textures=[3]),
    _dump(_TRIANGLE, [(0, 1, 2)], [(1, 1, 1)], texture_faces=[0], face_textures=[3]),  # no triangles at all
    _dump(_TRIANGLE, [(0, 1, 2)], [_FLAT], compress=False),
    gzip.compress(b""),
    gzip.compress(struct.pack(">i", pm.RAW_MAGIC)),
    gzip.compress(struct.pack(">i", pm.MAGIC) + bytes(64)),
    b"\x1f\x8b" + bytes(32),
    _dump(_TRIANGLE, [(0, 1, 2)], [_FLAT])[:-9],                              # cut short
    _dump(_TRIANGLE, [(0, 1, 2)], [_FLAT]) + b"more",
])
def test_a_dishonest_dump_is_refused(dump):
    with pytest.raises(pm.MeshError):
        pm.convert(dump)


def test_a_dump_cannot_expand_without_bound(monkeypatch):
    monkeypatch.setattr(pm, "MAX_RAW_BYTES", 4096)
    bomb = gzip.compress(bytes(1024 * 1024))
    assert len(bomb) < 4096
    with pytest.raises(pm.MeshError):
        pm.convert(bomb)


def test_too_many_or_too_large_textures_are_refused(monkeypatch):
    monkeypatch.setattr(pm, "MAX_TEXTURE_PIXELS", 4)
    with pytest.raises(pm.MeshError):
        pm.convert(_dump(_TRIANGLE, [(0, 1, 2)], [_FLAT], textures=[(1, 0, 0, [1] * 9)]))
    monkeypatch.setattr(pm, "MAX_TEXTURES", 1)
    with pytest.raises(pm.MeshError):
        pm.convert(_dump(_TRIANGLE, [(0, 1, 2)], [_FLAT], textures=[(1, 0, 0, None), (2, 0, 0, None)]))


def test_a_degenerate_texture_triangle_is_not_an_error():
    # All three texture corners on one vertex: the plugin divided by zero too.
    glb = pm.convert(_dump(_TRIANGLE, [(0, 1, 2)], [(1, 1, 1)], texture_faces=[0], tex=[(1, 1, 1)],
                           face_textures=[3], textures=[(3, 0, 0, [5])]))
    assert glb[:4] == b"glTF"


# --------------------------------------------------------------------------- #
# The upload route
# --------------------------------------------------------------------------- #
class TestRoute:
    @pytest.fixture
    def route(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "services.player_model_mesh", pm)
        return _load("_player_model_route_for_raw", "api", "routes", "player_model.py")

    async def _post(self, route, monkeypatch, files):
        from quart import Quart
        from werkzeug.datastructures import FileStorage

        seen = {}

        def fake_store(acc_hash, fingerprint, model_bytes, pet_bytes, pin=False):
            seen.update(model=model_bytes, pet=pet_bytes)
            return {"stored": False, "player_id": 1, "url": "u", "pinned": False}

        monkeypatch.setattr(route, "_store", fake_store)
        app = Quart(__name__)
        app.register_blueprint(route.player_model_bp)
        resp = await app.test_client().post(
            "/player/model", form={"acc_hash": "h", "fingerprint": "abcd1234"},
            files={k: FileStorage(io.BytesIO(v), filename=k) for k, v in files.items()})
        return resp, seen

    async def test_a_model_dump_is_stored_as_the_glb_it_makes(self, route, monkeypatch):
        dump = _dump(_TRIANGLE, [(0, 1, 2)], [_FLAT])
        resp, seen = await self._post(route, monkeypatch, {"mesh": dump, "pet_mesh": dump})
        assert resp.status_code == 200
        assert _gltf(seen["model"])["nodes"][0]["name"] == "player"
        assert _gltf(seen["pet"])["nodes"][0]["name"] == "pet"

    async def test_a_bad_dump_is_invalid_but_a_bad_pet_only_drops_the_pet(self, route, monkeypatch):
        resp, seen = await self._post(route, monkeypatch, {"mesh": gzip.compress(b"junk")})
        assert resp.status_code == 422 and (await resp.get_json())["reason"] == "invalid_model"
        resp, seen = await self._post(route, monkeypatch, {"mesh": _dump(_TRIANGLE, [(0, 1, 2)], [_FLAT]),
                                                           "pet_mesh": gzip.compress(b"junk")})
        assert resp.status_code == 200 and seen["pet"] is None

    async def test_the_stored_file_passes_the_model_validator(self, route, monkeypatch):
        player_model = _load("_real_player_model_for_raw", "services", "player_model.py")
        glb = pm.convert(_fixture("raw_0.dtm2"), "player")
        ok, why = player_model.validate_glb(glb)
        assert ok, why
