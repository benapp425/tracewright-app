"""Make KiCad's GLB export light enough to orbit: one primitive per mesh and material.

KiCad writes every track segment, pad, via and silkscreen stroke as its own primitive (34 000 on a
4-layer board with 160 parts), and a browser pays a draw call for each one, every frame. Merging
the primitives that share a mesh and a material keeps the node tree as it was (each part stays a
node named by its reference, so picking and highlighting work unchanged) and cuts the draw calls to
a few hundred.

    optimize("board.glb", "board-light.glb")   -> {"primitives": [before, after], "bytes": [before, after]}
"""
import json, struct, os, re
import numpy as np

COMP = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
KEEP = ("POSITION", "NORMAL", "TEXCOORD_0", "COLOR_0")


def read_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        raise ValueError("not a binary glTF file")
    off, js, binary = 12, None, b""
    while off < length:
        clen, ctype = struct.unpack_from("<II", data, off)
        chunk = data[off + 8: off + 8 + clen]
        if ctype == 0x4E4F534A:
            js = json.loads(chunk.decode("utf-8"))
        elif ctype == 0x004E4942:
            binary = chunk
        off += 8 + clen
    return js, binary


def accessor(js, binary, i):
    """An accessor's data as an array of shape (count, components)."""
    a = js["accessors"][i]
    if a.get("sparse"):
        raise ValueError("sparse accessor")
    dt = np.dtype(COMP[a["componentType"]])
    n = NCOMP[a["type"]]
    count = a["count"]
    if "bufferView" not in a:
        return np.zeros((count, n), dt)
    v = js["bufferViews"][a["bufferView"]]
    start = v.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = v.get("byteStride") or dt.itemsize * n
    row = dt.itemsize * n
    if stride == row:
        return np.frombuffer(binary, dt, count * n, start).reshape(count, n)
    raw = np.frombuffer(binary, np.uint8, stride * (count - 1) + row, start)          # interleaved: gather the rows
    rows = np.lib.stride_tricks.as_strided(raw, shape=(count, row), strides=(stride, 1))
    return np.ascontiguousarray(rows).view(dt).reshape(count, n)


class _Writer:
    def __init__(self):
        self.bin = bytearray()
        self.views, self.accessors = [], []

    def add(self, arr, comp, typ, target=None, minmax=False, normalized=False):
        arr = np.ascontiguousarray(arr)
        while len(self.bin) % 4:
            self.bin.append(0)
        view = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": arr.nbytes}
        if target:
            view["target"] = target
        self.bin += arr.tobytes()
        self.views.append(view)
        acc = {"bufferView": len(self.views) - 1, "componentType": comp, "count": int(arr.shape[0]), "type": typ}
        if normalized:
            acc["normalized"] = True
        if minmax:
            acc["min"] = [float(x) for x in arr.min(axis=0)]
            acc["max"] = [float(x) for x in arr.max(axis=0)]
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def raw(self, data):
        while len(self.bin) % 4:
            self.bin.append(0)
        self.views.append({"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)})
        self.bin += data
        return len(self.views) - 1


def _copy_accessor(js, binary, w, i, target=None):
    a = js["accessors"][i]
    arr = accessor(js, binary, i)
    return w.add(arr, a["componentType"], a["type"], target, minmax="min" in a, normalized=a.get("normalized", False))


def optimize(src, dst):
    js, binary = read_glb(src)
    w = _Writer()
    before = sum(len(m.get("primitives", [])) for m in js.get("meshes", []))
    for mesh in js.get("meshes", []):
        groups, order, keep = {}, [], []
        for p in mesh.get("primitives", []):
            attrs = p.get("attributes", {})
            plain = p.get("mode", 4) == 4 and not p.get("targets") and not p.get("extensions") and "POSITION" in attrs \
                and all(not js["accessors"][a].get("sparse") for a in attrs.values())
            if not plain:
                keep.append(p)
                continue
            key = (p.get("material"), tuple(sorted(k for k in attrs if k in KEEP)))
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(p)
        prims = []
        for key in order:
            mat, names = key
            cols = {k: [] for k in names}
            idx, base = [], 0
            for p in groups[key]:
                attrs = p["attributes"]
                pos = accessor(js, binary, attrs["POSITION"])
                n = pos.shape[0]
                for k in names:
                    arr = accessor(js, binary, attrs[k])
                    if k == "COLOR_0" and arr.dtype != np.float32:        # normalised ints -> floats
                        arr = arr.astype(np.float32) / float(np.iinfo(arr.dtype).max)
                    if k == "COLOR_0" and arr.shape[1] == 3:
                        arr = np.hstack([arr, np.ones((n, 1), np.float32)])
                    cols[k].append(arr.astype(np.float32, copy=False))
                if "indices" in p:
                    ix = accessor(js, binary, p["indices"]).reshape(-1).astype(np.uint32)
                else:
                    ix = np.arange(n, dtype=np.uint32)
                idx.append(ix + base)
                base += n
            out = {"attributes": {}, "mode": 4}
            for k in names:
                arr = np.concatenate(cols[k]) if len(cols[k]) > 1 else cols[k][0]
                out["attributes"][k] = w.add(arr, 5126, {2: "VEC2", 3: "VEC3", 4: "VEC4"}[arr.shape[1]], 34962,
                                             minmax=(k == "POSITION"))
            ix = np.concatenate(idx) if len(idx) > 1 else idx[0]
            if base < 65536:
                out["indices"] = w.add(ix.astype(np.uint16).reshape(-1, 1), 5123, "SCALAR", 34963)
            else:
                out["indices"] = w.add(ix.reshape(-1, 1), 5125, "SCALAR", 34963)
            if mat is not None:
                out["material"] = mat
            prims.append(out)
        for p in keep:                                             # anything unusual is copied as it was
            q = dict(p)
            q["attributes"] = {k: _copy_accessor(js, binary, w, a, 34962) for k, a in p.get("attributes", {}).items()}
            if "indices" in p:
                q["indices"] = _copy_accessor(js, binary, w, p["indices"], 34963)
            if p.get("targets"):
                q["targets"] = [{k: _copy_accessor(js, binary, w, a) for k, a in t.items()} for t in p["targets"]]
            prims.append(q)
        mesh["primitives"] = prims
    for node in js.get("nodes", []):                               # the board's own layers keep their names
        m = node.get("mesh")
        name = js["meshes"][m].get("name", "") if m is not None else ""
        if re.search(r"_(PCB|copper|pad|via|silkscreen|soldermask|paste)$", name) and str(node.get("name", "=>")).startswith("=>"):
            node["name"] = name
    for img in js.get("images", []):                               # embedded textures, if any
        if "bufferView" in img:
            v = js["bufferViews"][img["bufferView"]]
            img["bufferView"] = w.raw(binary[v.get("byteOffset", 0): v.get("byteOffset", 0) + v["byteLength"]])
    js["accessors"], js["bufferViews"] = w.accessors, w.views
    js["buffers"] = [{"byteLength": len(w.bin)}]
    js.setdefault("asset", {"version": "2.0"})["generator"] = (js["asset"].get("generator", "") + " + tracewright glbopt").strip()
    body = json.dumps(js, separators=(",", ":")).encode("utf-8")
    body += b" " * ((4 - len(body) % 4) % 4)
    while len(w.bin) % 4:
        w.bin.append(0)
    total = 12 + 8 + len(body) + 8 + len(w.bin)
    tmp = dst + ".part"
    with open(tmp, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(body), 0x4E4F534A))
        f.write(body)
        f.write(struct.pack("<II", len(w.bin), 0x004E4942))
        f.write(w.bin)
    os.replace(tmp, dst)
    after = sum(len(m.get("primitives", [])) for m in js.get("meshes", []))
    return {"primitives": [before, after], "bytes": [os.path.getsize(src), os.path.getsize(dst)]}
