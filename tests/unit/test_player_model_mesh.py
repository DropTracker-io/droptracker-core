"""services/player_model_mesh.py: the plugin's mesh dump to the stored glTF.

Plugin 6.1.15 sends its mesh and the server writes the glTF the plugin's
``GlbWriter`` used to. What these pin:

* the output is byte-for-byte what the Java writer produced for the same mesh
  (fixtures written by both from one mesh; 300 random meshes matched when
  this was built);
* meshes past 65,535 vertices switch to 32-bit indices, as the Java one did;
* a dump that lies about its sizes, points outside its buffers or carries
  extra bytes is refused before anything is allocated for it;
* the upload route accepts either form and never loses a player's model over
  a bad pet.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import struct
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FIXTURES = os.path.join(_ROOT, "tests", "fixtures", "player_model_mesh")


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pm = _load("_real_player_model_mesh", "services", "player_model_mesh.py")


def _fixture(name):
    with open(os.path.join(_FIXTURES, name), "rb") as fh:
        return fh.read()


def _dump(vertices=3, indices=None, prims=None, uvs=True, extra=b"", colors=None, name="player"):
    """A DTM1 dump the way GlbExporter writes one (big-endian)."""
    indices = indices if indices is not None else [0, 1, 2]
    prims = prims if prims is not None else [(-1, 0, len(indices), False, 0, None)]
    out = bytearray(struct.pack(">i", pm.MAGIC))
    raw = name.encode()
    out += struct.pack(">H", len(raw)) + raw + struct.pack(">i", 12)

    def floats(values):
        return struct.pack(">i", -1) if values is None else struct.pack(f">i{len(values)}f", len(values), *values)

    out += floats([float(i % 7) for i in range(3 * vertices)])
    color_bytes = colors if colors is not None else bytes(4 * vertices)
    out += struct.pack(">i", len(color_bytes)) + color_bytes
    out += floats([0.5] * (2 * vertices) if uvs else None) + floats(None)
    out += struct.pack(f">i{len(indices)}i", len(indices), *indices)
    out += floats([0.0, 0.0, 0.0]) + floats([6.0, 6.0, 6.0])
    out += struct.pack(">i", len(prims))
    for tid, offset, count, translucent, state, png in prims:
        out += struct.pack(">iii?B", tid, offset, count, translucent, state)
        if state == 1:
            out += struct.pack(">iffi", 0x336699, 0.0, 0.0, -1 if png is None else len(png)) + (png or b"")
    return bytes(out) + extra


@pytest.mark.parametrize("name", ["textured", "untextured"])
def test_the_glb_is_byte_for_byte_what_the_plugin_wrote(name):
    assert pm.convert(_fixture(f"{name}.dtm")) == _fixture(f"{name}.glb")


def _gltf(glb):
    length = struct.unpack("<I", glb[12:16])[0]
    return json.loads(glb[20:20 + length])


def test_big_meshes_get_32_bit_indices():
    n = pm._MAX_SHORT_INDEX + 10
    glb = pm.convert(_dump(vertices=n, indices=[0, 1, n - 1], uvs=False))
    gltf = _gltf(glb)
    assert gltf["accessors"][-1]["componentType"] == 5125
    assert gltf["asset"]["extras"] == {"source": "droptracker-plugin", "faceCount": 12, "vertexCount": n}
    small = _gltf(pm.convert(_dump()))
    assert small["accessors"][-1]["componentType"] == 5123


def test_a_texture_shared_by_two_primitives_is_embedded_once():
    png = b"\x89PNG fake"
    glb = pm.convert(_dump(indices=[0, 1, 2, 2, 1, 0],
                           prims=[(4, 0, 3, False, 1, png), (4, 3, 3, True, 2, None)]))
    gltf = _gltf(glb)
    assert len(gltf["images"]) == 1 and len(gltf["materials"]) == 2
    assert [m["alphaMode"] for m in gltf["materials"]] == ["MASK", "BLEND"]


@pytest.mark.parametrize("dump", [
    b"",
    b"glTF" + bytes(40),
    _dump()[:-3],                                       # truncated
    _dump(extra=b"\0"),                                 # trailing bytes
    _dump(indices=[0, 1, 3]),                           # index past the vertices
    _dump(indices=[0, 1, -1]),
    _dump(prims=[(-1, 0, 6, False, 0, None)]),          # primitive past the indices
    _dump(prims=[(5, 0, 3, False, 2, None)]),           # "sent earlier" but never sent
    _dump(prims=[(5, 0, 3, False, 7, None)]),           # unknown texture state
    _dump(colors=bytes(5)),                             # colours that don't match the vertices
])
def test_a_dishonest_dump_is_refused(dump):
    with pytest.raises(pm.MeshError):
        pm.convert(dump)


def test_counts_are_checked_before_reading():
    huge = bytearray(_dump())
    # The position count sits after magic(4), name(2+6) and face count(4).
    struct.pack_into(">i", huge, 16, 2_000_000_000)
    with pytest.raises(pm.MeshError):
        pm.parse(bytes(huge))


# --------------------------------------------------------------------------- #
# The upload route
# --------------------------------------------------------------------------- #
class TestRoute:
    @pytest.fixture
    def route(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "services.player_model_mesh", pm)
        return _load("_player_model_route_for_mesh", "api", "routes", "player_model.py")

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

    async def test_a_mesh_is_stored_as_the_glb_it_makes(self, route, monkeypatch):
        resp, seen = await self._post(route, monkeypatch, {"mesh": _fixture("textured.dtm"),
                                                           "pet_mesh": _fixture("untextured.dtm")})
        assert resp.status_code == 200
        assert seen == {"model": _fixture("textured.glb"), "pet": _fixture("untextured.glb")}

    async def test_a_bad_mesh_is_invalid_but_a_bad_pet_only_drops_the_pet(self, route, monkeypatch):
        resp, seen = await self._post(route, monkeypatch, {"mesh": b"junk"})
        assert resp.status_code == 422 and (await resp.get_json())["reason"] == "invalid_model"
        resp, seen = await self._post(route, monkeypatch, {"mesh": _fixture("textured.dtm"), "pet_mesh": b"junk"})
        assert resp.status_code == 200 and seen["pet"] is None

    async def test_older_plugins_still_send_the_glb(self, route, monkeypatch):
        resp, seen = await self._post(route, monkeypatch, {"model": b"glTF-bytes"})
        assert resp.status_code == 200 and seen["model"] == b"glTF-bytes"
