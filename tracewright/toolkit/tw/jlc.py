"""JLCPCB / LCSC part data with a dated on-disk cache, and CPL placement against JLC's footprints.

    ./tw parts search "AMS1117-3.3"       JLC parts search (JLC assembly stock, Basic / Extended)
    ./tw parts code C6186 C1525           LCSC details (LCSC stock, price, parameters)

Every answer is cached in sourcing/cache/ with the UTC time of the query, so a stock claim in the
BOM can be traced to the query behind it. JLC's search reports the JLC assembly warehouse; LCSC's
detail API reports the LCSC warehouse -- different pools, both recorded.

JLC places each part by its *own* library footprint for the LCSC code (EasyEDA's), turned by the
CPL rotation about that footprint's origin. `fit_placements` fetches that footprint, fits its
pads onto our pads by pad number (rotation + shift, no mirror) and derives the correction the
CPL needs. Pad-number traps (JLC numbers LED anodes 1; KiCad numbers cathodes 1) are matched by
pin name instead.
"""
import os, json, time, hashlib, subprocess, datetime, math, statistics, urllib.request, ssl

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MAX_AGE_H = 24.0
UNIT = 0.254                      # EasyEDA canvas unit: 10 mil


class Cache:
    def __init__(self, root):
        self.dir = os.path.join(root, "sourcing", "cache")

    def path(self, kind, key):
        return os.path.join(self.dir, f"{kind}_{hashlib.sha1(key.encode()).hexdigest()[:16]}.json")

    def get(self, kind, key, refresh=False, max_age_h=MAX_AGE_H):
        p = self.path(kind, key)
        if refresh or not os.path.exists(p):
            return None
        try:
            with open(p) as f:
                d = json.load(f)
        except ValueError:
            return None
        if max_age_h is not None and (time.time() - d.get("_epoch", 0)) / 3600 > max_age_h:
            return None
        return d

    def put(self, kind, key, payload):
        os.makedirs(self.dir, exist_ok=True)
        payload.update({"_query": key, "_kind": kind, "_epoch": time.time(),
                        "_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        with open(self.path(kind, key), "w") as f:
            json.dump(payload, f, indent=1)
        return payload


class LookupFailed(RuntimeError):
    """A parts service did not answer (network, throttling, time budget): nothing is known, which is
    different from an answer that says "no such footprint"."""


def _http(url, body=None, headers=None, timeout=40):
    """GET / POST JSON. curl (uses the system trust store) when it exists, urllib otherwise. One try,
    bounded by `timeout` seconds end to end; raises LookupFailed."""
    timeout = max(1.0, float(timeout))
    hdr = {"User-Agent": UA, "Accept": "application/json, text/javascript, */*; q=0.01"}
    hdr.update(headers or {})
    args = ["curl", "-sS", "-m", f"{timeout:.0f}", "--connect-timeout", f"{min(timeout, 10):.0f}", "--compressed"]
    for k, v in hdr.items():
        args += ["-H", f"{k}: {v}"]
    if body is not None:
        args += ["-X", "POST", "-H", "Content-Type: application/json", "-d", json.dumps(body)]
    try:
        r = subprocess.run(args + [url], capture_output=True, text=True, timeout=timeout + 5)
    except FileNotFoundError:
        r = None                                   # no curl: urllib below
    except subprocess.TimeoutExpired:
        raise LookupFailed(f"no answer in {timeout:.0f} s")
    if r is not None:
        if r.returncode != 0:
            raise LookupFailed((r.stderr or f"curl exit {r.returncode}").strip().splitlines()[-1][:160])
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise LookupFailed("the answer was not JSON (throttled or blocked?)")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=hdr)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        raise LookupFailed(f"{type(e).__name__}: {e}"[:160])


class Parts:
    """JLC / LCSC / EasyEDA lookups with the on-disk cache. `deadline` (epoch seconds) bounds the
    network time of a whole run and `stop` (a threading.Event) ends it early: past either, a lookup
    that is not cached raises LookupFailed at once. A code that failed is not asked again by the
    same Parts (a board with 40 resistors of one code must not wait 40 times)."""

    def __init__(self, root, deadline=None, stop=None, timeout=15.0):
        self.cache = Cache(root)
        self.deadline = deadline
        self.stop = stop
        self.timeout = timeout
        self.failed = {}                       # (kind, key) -> reason

    def _budget(self, kind, key):
        """Seconds this lookup may take, or LookupFailed when the run has none left."""
        if (kind, key) in self.failed:
            raise LookupFailed(self.failed[(kind, key)])
        if self.stop is not None and self.stop.is_set():
            raise LookupFailed("the run was stopped")
        left = self.timeout if self.deadline is None else min(self.timeout, self.deadline - time.time())
        if left < 1.0:
            raise LookupFailed("out of lookup time for this run")
        return left

    def _get(self, kind, key, url, body=None, tries=2):
        """_http with the budget, a short back-off between tries, and the failure remembered."""
        last = None
        for attempt in range(tries):
            try:
                return _http(url, body, timeout=self._budget(kind, key))
            except LookupFailed as e:
                last = e
                if "out of lookup time" in str(e) or "stopped" in str(e):
                    break
                if attempt + 1 < tries:
                    time.sleep(1.5 * (attempt + 1))
        self.failed[(kind, key)] = str(last)
        raise LookupFailed(str(last))

    def search(self, keyword, page_size=25, refresh=False):
        key = f"{keyword}|{page_size}"
        c = self.cache.get("jlc", key, refresh)
        if c:
            return c
        d = self._get("jlc", key, "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList",
                      {"keyword": keyword, "currentPage": 1, "pageSize": page_size})
        items = []
        for it in ((d.get("data") or {}).get("componentPageInfo") or {}).get("list") or []:
            prices = sorted(it.get("componentPrices") or [], key=lambda p: p.get("startNumber", 0))
            items.append({"lcsc": it.get("componentCode"), "mpn": it.get("componentModelEn"),
                          "brand": it.get("componentBrandEn"), "package": it.get("componentSpecificationEn"),
                          "jlc_stock": it.get("stockCount"),
                          "lib": {"base": "Basic", "expand": "Extended"}.get(it.get("componentLibraryType"),
                                                                               it.get("componentLibraryType")),
                          "preferred": it.get("preferredComponentFlag"),
                          "price_1": prices[0].get("productPrice") if prices else None,
                          "no_buy_reason": it.get("noBuyReason"), "describe": it.get("describe"),
                          "datasheet": it.get("dataManualOfficialLink") or it.get("dataManualUrl")})
        return self.cache.put("jlc", key, {"items": items})

    def detail(self, code, refresh=False, max_age_h=MAX_AGE_H):
        c = self.cache.get("lcsc", code, refresh, max_age_h=max_age_h)
        if c:
            return c
        d = self._get("lcsc", code, f"https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={code}")
        r = d.get("result") or {}
        prices = r.get("productPriceList") or []
        return self.cache.put("lcsc", code, {
            "lcsc": r.get("productCode"), "mpn": r.get("productModel"), "brand": r.get("brandNameEn"),
            "name": r.get("productNameEn"), "category": r.get("catalogName"),
            "package": r.get("encapStandard"), "lcsc_stock": r.get("stockNumber"),
            "price_1": prices[0].get("usdPrice") if prices else None, "discontinued": r.get("isDiscontinued"),
            "datasheet": r.get("pdfUrl"), "description": r.get("productIntroEn"),
            "params": {p.get("paramNameEn"): p.get("paramValueEn") for p in (r.get("paramVOList") or [])}})

    def easyeda(self, code, refresh=False):
        """JLC's own footprint + symbol for an LCSC code (raw 'result'); {} when EasyEDA has none.
        Raises LookupFailed when EasyEDA does not answer (its CDN refuses bursts)."""
        c = self.cache.get("easyeda", code, refresh, max_age_h=None)
        if c:
            return c
        d = self._get("easyeda", code, f"https://easyeda.com/api/products/{code}/components?version=6.4.19.5")
        if not isinstance(d, dict) or ("result" not in d and d.get("success") is False):
            self.failed[("easyeda", code)] = "EasyEDA refused the request"
            raise LookupFailed("EasyEDA refused the request")
        time.sleep(0.3)                               # pace the next request
        return self.cache.put("easyeda", code, {"result": d.get("result") or {}})

    def jlc_footprint(self, code, refresh=False):
        """({pad number: [(x, y) mm, y up, about the footprint origin]}, footprint title, {pin: name})."""
        r = self.easyeda(code, refresh).get("result") or {}
        ds = ((r.get("packageDetail") or {}).get("dataStr")) or {}
        head = ds.get("head") or {}
        ox, oy = float(head.get("x", 0) or 0), float(head.get("y", 0) or 0)
        pads = {}
        for s in ds.get("shape") or []:
            if not s.startswith("PAD~"):
                continue
            f = s.split("~")
            try:
                x, y, num = float(f[2]), float(f[3]), f[8]
            except (IndexError, ValueError):
                continue
            pads.setdefault(num, []).append(((x - ox) * UNIT, -(y - oy) * UNIT))
        names = {}
        for s in (r.get("dataStr") or {}).get("shape") or []:
            if s.startswith("P~"):
                parts = s.split("^^")
                try:
                    num = parts[0].split("~")[3]
                    f = parts[3].split("~") if len(parts) > 3 else []
                    names[num] = f[4] if len(f) > 4 else ""
                except IndexError:
                    continue
        return pads, (r.get("packageDetail") or {}).get("title", ""), names


# ----------------------------------------------------------------------------- CPL fitting
CATHODE, ANODE = ("K", "C", "-", "CATHODE", "KA"), ("A", "+", "ANODE")
# 2-pin parts whose marking matters though they are not polarised: {LCSC: {JLC pad: our pad}}.
# Coilcraft XAL1010 (doc 804-4): the top stripe marks the start lead ("connect high dv/dt here");
# JLC's footprint draws it beside its pad 2.
MARKED = {"C19271849": {"2": "1", "1": "2"}, "C3911672": {"2": "1", "1": "2"}}
PLUS_PAD = {"C165636": "1"}           # JLC symbols that do not name the + pin (silk shows it)
ROT_TOL, POS_TOL, FIT_TOL, SYM_TOL, SNAP = 1.0, 0.05, 0.40, 0.60, 3.0


def polarised(ref, fp_lib=""):
    r = ref.rstrip("0123456789")
    lib = fp_lib.lower()
    return r in ("D", "LED", "BT", "CR", "ZD") or r.startswith(("D", "LED")) or \
        (r == "C" and any(k in lib for k in ("cp_", "elec", "tantal", "pol")))


def fit(src, dst):
    """Turn (degrees CCW, y up) and shift taking src points onto dst, and the worst residual."""
    n = len(src)
    sx = sum(p[0] for p in src) / n
    sy = sum(p[1] for p in src) / n
    dx = sum(p[0] for p in dst) / n
    dy = sum(p[1] for p in dst) / n
    a = b = 0.0
    for (px, py), (qx, qy) in zip(src, dst):
        px, py, qx, qy = px - sx, py - sy, qx - dx, qy - dy
        a += px * qx + py * qy
        b += px * qy - py * qx
    th = math.atan2(b, a)
    c, s = math.cos(th), math.sin(th)
    T = lambda p: (dx + c * (p[0] - sx) - s * (p[1] - sy), dy + s * (p[0] - sx) + c * (p[1] - sy))
    res = max(math.hypot(T(p)[0] - q[0], T(p)[1] - q[1]) for p, q in zip(src, dst))
    return math.degrees(th), T((0.0, 0.0)), res


def wrap(a):
    return (a + 180.0) % 360.0 - 180.0


def our_number(ref, num, names, code, fp_lib, overrides):
    """Our pad number for JLC's pad `num`. tracewright.json fab.cpl_pad_map overrides per reference:
    {"J401": "reverse:22"} (pad k <-> 23 - k, e.g. the Raspberry Pi FH12 numbering), {"J202": "offset:100"}
    (JLC 1-100, ours 101-200), or an explicit {"11": "9", "12": "10", "13": null} (null: skip that pad)."""
    m = (overrides or {}).get(ref)
    if m is not None:
        if isinstance(m, dict):
            return m.get(num, num)
        if isinstance(m, str) and ":" in m and num.isdigit():
            kind, n = m.split(":", 1)
            if kind == "reverse" and 1 <= int(num) <= int(n):
                return str(int(n) + 1 - int(num))
            if kind == "offset":
                return str(int(num) + int(n))
        return num
    if code in MARKED:
        return MARKED[code].get(num)
    if polarised(ref, fp_lib) and ref.rstrip("0123456789") != "C" and not ref.startswith("BT"):
        n = names.get(num, "").upper()
        return "1" if n in CATHODE else ("2" if n in ANODE else None)
    if ref.startswith("BT") or polarised(ref, fp_lib):
        if code in PLUS_PAD:
            return "1" if num == PLUS_PAD[code] else "2"
        n = names.get(num, "").upper()
        return "1" if n in ("+", "POSITIVE", "PLUS") else ("2" if n in ("-", "NEGATIVE", "MINUS") else None)
    return num


def fit_placements(parts, board, cpl_rows, lcsc_of, overrides=None, refresh=False):
    """rows [(ref, code, jlc title, cpl rot, fitted rot, origin off, worst pad, verdict)] and the
    fitted {ref: (code, rot, origin, flip_ok)} for write_corrections."""
    fps = board.footprints
    pads = {}
    for fp in board.fp_list:
        for p in fp.pads:
            pads.setdefault(fp.ref, {}).setdefault(p.num, []).append((p.x, -p.y))    # y up like the CPL
    offs = [(float(r["Mid X"].rstrip("m")) - fps[d].x, float(r["Mid Y"].rstrip("m")) + fps[d].y)
            for d, r in cpl_rows.items() if d in fps]
    if not offs:
        return [], {}, (0.0, 0.0)
    down = [float(r["Mid Y"].rstrip("m")) - fps[d].y for d, r in cpl_rows.items() if d in fps]
    spread = lambda v: statistics.median(abs(x - statistics.median(v)) for x in v)
    ydown = len(down) >= 3 and spread(down) < 0.2 * spread([o[1] for o in offs])    # a CPL written with y down
    ox, oy = statistics.median(o[0] for o in offs), statistics.median(o[1] for o in offs)
    if ydown:
        oy = statistics.median(down)
    rows, fitted = [], {}
    for ref in sorted(cpl_rows):
        r = cpl_rows[ref]
        code = lcsc_of.get(ref, "")
        fp = fps.get(ref)
        if not fp:
            continue
        rot = float(r["Rotation"])
        my = float(r["Mid Y"].rstrip("m")) - oy
        mid = (float(r["Mid X"].rstrip("m")) - ox, -my if ydown else my)
        if not code:
            rows.append((ref, "", "", "", "", "", "", "no LCSC code"))
            continue
        try:
            ee, title, names = parts.jlc_footprint(code, refresh)
        except LookupFailed as e:
            rows.append((ref, code, "", "", "", "", "", f"lookup failed: {e}"))
            continue
        except Exception as e:
            rows.append((ref, code, "", "", "", "", "", f"lookup failed: {type(e).__name__}: {e}"))
            continue
        if not ee:
            rows.append((ref, code, title, "", "", "", "", "no JLC footprint to check against"))
            continue
        if r.get("Layer", "Top").lower().startswith("b"):
            rows.append((ref, code, title, "", "", "", "", "bottom side: check by hand (mirrored)"))
            continue
        ours = pads.get(ref, {})
        pairs = [(k, our_number(ref, k, names, code, fp.lib_id, overrides)) for k in ee if len(ee[k]) == 1]
        pairs = [(k, m) for k, m in pairs if m is not None and len(ours.get(m, [])) == 1]
        allours = [q for v in ours.values() for q in v]

        def land(th, org):
            c, s = math.cos(math.radians(th)), math.sin(math.radians(th))
            return max(min(math.hypot(org[0] + c * x - s * y - q[0], org[1] + s * x + c * y - q[1]) for q in allours)
                       for v in ee.values() for x, y in v)
        pol = polarised(ref, fp.lib_id)
        if len(pairs) < 2:
            sym = min(land(rot + t, mid) for t in (0.0, 180.0)) if allours else 99
            if sym <= SYM_TOL and not pol:
                rows.append((ref, code, title, f"{rot:.0f}", "sym", "0.00", f"{sym:.2f}", "ok"))
                fitted[ref] = (code, rot, mid, True)
                continue
            why = "polarity pins not named in JLC's symbol" if pol else "pad numbers do not match"
            rows.append((ref, code, title, "", "", "", "", f"{why} (JLC pads {sorted(ee)[:6]}, ours {sorted(ours)[:6]})"))
            continue
        th, org, _ = fit([ee[k][0] for k, m in pairs], [ours[m][0] for k, m in pairs])
        if abs(th - 90 * round(th / 90)) <= SNAP:
            th = 90 * round(th / 90)
        res = land(th, org)
        d = wrap(th - rot)
        flip_ok = len(pairs) == 2 and len(ee) == 2 and not pol and code not in MARKED
        fitted[ref] = (code, th, org, flip_ok)
        off = math.hypot(org[0] - mid[0], org[1] - mid[1])
        notes = []
        if abs(d) > ROT_TOL and not (flip_ok and abs(abs(d) - 180) <= ROT_TOL):
            notes.append(f"rotate by {d:+.0f}")
        if off > POS_TOL:
            notes.append(f"origin {off:.2f} mm off")
        if res > FIT_TOL:
            notes.append(f"pads differ from JLC's by up to {res:.2f} mm")
        rows.append((ref, code, title, f"{rot:.0f}", f"{wrap(th) % 360:.0f}", f"{off:.2f}", f"{res:.2f}",
                     "; ".join(notes) or "ok"))
    return rows, fitted, (ox, oy)


def write_corrections(fitted, board, path):
    """Per LCSC code: the turn to add to KiCad's rotation and the origin shift in the footprint frame."""
    out = {}
    for ref, (code, th, org, flip_ok) in sorted(fitted.items()):
        fp = board.footprints[ref]
        rk = fp.angle % 360
        pk = (fp.x, -fp.y)
        dth = wrap(th - rk)
        if flip_ok and abs(abs(dth) - 180) <= ROT_TOL:
            dth = 0.0
        c, s = math.cos(math.radians(-rk)), math.sin(math.radians(-rk))
        vx, vy = org[0] - pk[0], org[1] - pk[1]
        v = (c * vx - s * vy, s * vx + c * vy)
        if abs(dth) <= ROT_TOL and math.hypot(*v) <= 0.02:
            continue
        e = out.setdefault(code, {"footprint": fp.lib_id, "rot": round(dth, 1), "dx": round(v[0], 3), "dy": round(v[1], 3),
                                  "refs": []})
        if abs(wrap(e["rot"] - dth)) > ROT_TOL or math.hypot(e["dx"] - v[0], e["dy"] - v[1]) > 0.02:
            raise ValueError(f"{ref}: its correction differs from {e['refs']} with the same LCSC code {code}")
        e["refs"].append(ref)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"_about": "per LCSC code: degrees to add to KiCad's rotation and the origin shift (mm, footprint "
                             "frame, y up) that put JLC's own footprint on our pads (./tw cpl --write)",
                   "_date": datetime.date.today().isoformat(), **out}, f, indent=1)
    return out
