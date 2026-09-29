// The board in 3D: KiCad's own model of it (its GLB export: the board, copper, silkscreen, solder mask
// and each part's 3D model), drawn with three.js to orbit, pan and zoom. Click a part to see what it
// is, flag a spot, try another solder-mask colour, save a picture; the raytraced renders sit beside it.
import { h, clear, api, toast, btn, lightbox, fmtTime, menu, upload } from "./util.js";
import { icon } from "./icons.js";
import { FlagLayer, FlagTool, flagEditor } from "./review.js";

const enc = encodeURIComponent;
let lib = null;
async function three() {
  if (!lib) {
    const THREE = await import("three");
    const [{ OrbitControls }, { GLTFLoader }, { RoomEnvironment }] = await Promise.all([
      import("three/addons/controls/OrbitControls.js"), import("three/addons/loaders/GLTFLoader.js"), import("three/addons/environments/RoomEnvironment.js")]);
    lib = { THREE, OrbitControls, GLTFLoader, RoomEnvironment };
  }
  return lib;
}

const MASKS = [["green", "Green", 0x1d5a3a], ["black", "Black", 0x121212], ["matte-black", "Matte black", 0x1c1c1c], ["blue", "Blue", 0x173d80],
  ["red", "Red", 0x8e1b1b], ["white", "White", 0xe8e8e8], ["purple", "Purple", 0x4a2379], ["yellow", "Yellow", 0xc9a515]];
const VIEWS = { top: [0, 0.0001], bottom: [0, Math.PI - 0.0001], front: [0, 1.02], iso: [0.62, 0.95], left: [-Math.PI / 2, 1.1], right: [Math.PI / 2, 1.1] };

export class Viewer3D {
  constructor(el, ws) {
    this.el = el; this.ws = ws; this.pid = ws.pid;
    this.ready = false; this.isStale = true; this.parts = true; this.mask = null; this.showMask = true;
    this.build();
  }

  build() {
    this.loading = h("div.v3d-load", h("span.spinner.lg"), h("div", "Starting the 3D view…"));
    this.info = h("div.selbar", { style: { display: "none" } });
    this.tip = h("div.vtip", { style: { display: "none" } });
    this.flagBtn = h("button.tbtn", { onclick: () => this.flags && this.flags.toggle(), "data-tip": "Flag a spot", "data-kbd": "c" }, icon("flag", 15));
    this.partsBtn = h("button.tbtn.on", { onclick: () => this.setParts(!this.parts), "data-tip": "Toggle parts", "data-kbd": "p" }, icon("microchip", 15));
    this.maskBtn = h("button.tbtn", { onclick: (e) => this.maskMenu(e.currentTarget), "data-tip": "Solder mask color" }, icon("layers", 15), icon("chevron-down", 11));
    this.modeSeg = h("div.hudbox",
      h("button.tbtn.on", { onclick: () => this.renders(false), "data-mode": "model" }, icon("rotate-3d", 14), h("span", "Model")),
      h("button.tbtn", { onclick: () => this.renders(true), "data-mode": "renders" }, icon("camera", 14), h("span", "Renders")));
    const vbtn = (k, label, ic) => h("button.tbtn", { onclick: () => this.view(k), "data-tip": label + " view" }, ic ? icon(ic, 15) : h("span", label));
    this.hudTc = h("div.hud.tc");
    this.stage = h("div.viewer.v3d", this.loading,
      h("div.hud.tl", this.modeSeg),
      h("div.hud.tr", h("div.hudbox", vbtn("top", "Top"), vbtn("bottom", "Bottom"), vbtn("front", "Front"), vbtn("iso", "Iso"), h("div.tsep"),
        this.partsBtn, this.maskBtn, h("div.tsep"), this.flagBtn, h("div.tsep"),
        h("button.tbtn", { onclick: () => this.savePicture(), "data-tip": "Save image" }, icon("camera", 15)),
        h("button.tbtn", { onclick: () => this.view(this.lastView || "iso"), "data-tip": "Fit the board", "data-kbd": "f" }, icon("scan", 15)))),
      this.hudTc,
      h("div.hud.bl", h("div.hudbox", h("div.coords", h("span", "Drag to orbit"), h("span", "right-drag to pan"), h("span", "scroll to zoom")))),
      h("div.hud.br", this.info), this.tip);
    this.el.appendChild(this.stage);
    this.gallery = h("div.v3d-renders", { style: { display: "none" } });
    this.el.appendChild(this.gallery);
    this.keys = (e) => {
      if (!this.el.classList.contains("on") || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (document.querySelector(".modal-bg, .popover, .palette-bg") || !this.ready) return;
      const k = e.key.toLowerCase();
      if (k === "f") this.view(this.lastView || "iso");
      else if (k === "c") this.flags.toggle();
      else if (k === "p") this.setParts(!this.parts);
      else if (k === "t") this.view("top");
      else if (k === "b") this.view("bottom");
      else if (k === "i") this.view("iso");
      else if (e.key === "Escape") { if (this.flags.active) this.flags.toggle(false); else this.select(null); }
      else return;
      e.preventDefault();
    };
    document.addEventListener("keydown", this.keys);
  }

  destroy() {
    document.removeEventListener("keydown", this.keys);
    cancelAnimationFrame(this.raf);
    if (this.ro) this.ro.disconnect();
    if (this.renderer) { this.renderer.dispose(); this.renderer.forceContextLoss && this.renderer.forceContextLoss(); }
    this.flagLayer && this.flagLayer.destroy();
  }

  async shown() {
    if (!this.ready) await this.init();
    if (this.isStale) this.load();
    this.resize();
  }

  hidden() { this.dragging = false; }
  stale() { this.isStale = true; if (this.el.classList.contains("on")) { clearTimeout(this.staleT); this.staleT = setTimeout(() => this.load(), 800); } }

  status(text, progress) {
    this.loading.style.display = "flex";
    clear(this.loading).append(h("span.spinner.lg"), h("div", text),
      progress !== undefined ? h("div.progress", h("i", { style: { width: Math.round(progress * 100) + "%" } })) : null);
  }

  async init() {
    let L;
    try { L = await three(); } catch (e) { this.status("3D view unavailable: " + e.message); return; }
    const { THREE, OrbitControls, RoomEnvironment } = L;
    this.T = THREE;
    let renderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" }); }
    catch (e) { clear(this.loading).append(icon("triangle-alert", 22), h("div", "3D view unavailable: WebGL is not supported."), h("div.small", "Use Renders instead.")); return; }
    this.maxDpr = Math.min(2, window.devicePixelRatio || 1);
    this.dpr = this.maxDpr;
    renderer.setPixelRatio(this.dpr);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 0.9;
    this.renderer = renderer;
    this.canvas = renderer.domElement;
    this.canvas.__view = this;                         // for debugging from the console
    this.stage.insertBefore(this.canvas, this.stage.firstChild);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x121316);
    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environmentIntensity = 0.75;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x202028, 0.25));
    const sun = new THREE.DirectionalLight(0xffffff, 0.9);
    sun.position.set(0.3, 1, 0.5);
    scene.add(sun);
    this.scene = scene;
    this.camera = new THREE.PerspectiveCamera(32, 1, 0.0005, 20);
    const controls = new OrbitControls(this.camera, this.canvas);
    controls.enableDamping = true; controls.dampingFactor = 0.12; controls.screenSpacePanning = true; controls.zoomToCursor = true;
    controls.rotateSpeed = 0.8; controls.zoomSpeed = 1.1;
    controls.addEventListener("change", () => this.dirty());
    controls.addEventListener("start", () => { this.dragging = true; this.tip.style.display = "none"; this.moving(true); });
    controls.addEventListener("end", () => { this.dragging = false; this.moving(false); });
    this.controls = controls;
    this.raycaster = new THREE.Raycaster();
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(this.stage);
    this.flagLayer = new FlagLayer(this.stage, this.ws.review, {
      view: "3d", project: (x, y, w) => this.project(x, y, w),
      onPin: (f, pin) => { this.flagLayer.mark(f.id); flagEditor(pin, this.ws, { flag: f, onDone: () => this.flagLayer.mark(null) }); } });
    this.flags = new FlagTool(this.ws, {
      view: "3d", surface: this.canvas, layer: this.flagLayer, hud: this.hudTc,
      toWorld: (px, py) => this.pickWorld(px, py), context: (w) => w, snapshot: (w) => this.snapshot(w),
      onChange: (on) => { this.flagBtn.classList.toggle("on", on); this.canvas.style.cursor = on ? "crosshair" : ""; this.controls.enabled = !on; } });
    this.pointer();
    this.ready = true;
    this.loop();
  }

  async load() {
    if (!this.ready) return;
    this.isStale = false;
    this.status("Exporting the 3D model…");
    const { GLTFLoader } = lib;
    const url = `/api/projects/${enc(this.pid)}/model.glb?t=${Date.now()}`;
    if (!this.boardData()) api(`/api/projects/${enc(this.pid)}/board`).then((b) => { if (!b.empty) this.board = b; }).catch(() => {});
    let gltf;
    try {
      const r = await fetch(url);
      if (r.status === 401) { location.href = "/"; return; }
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        if (r.status === 404) { this.empty(); return; }
        throw new Error(e.error || r.statusText);
      }
      const total = +r.headers.get("content-length") || 0;
      const reader = r.body.getReader();
      const chunks = []; let got = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value); got += value.length;
        if (total) this.status("Loading the model…", got / total);
      }
      const buf = new Uint8Array(got); let o = 0;
      for (const c of chunks) { buf.set(c, o); o += c.length; }
      this.status("Preparing the model…");
      gltf = await new Promise((res, rej) => new GLTFLoader().parse(buf.buffer, "", res, rej));
    } catch (e) {
      clear(this.loading).append(icon("triangle-alert", 22), h("div", "Couldn't create the 3D model"), h("div.small", { style: { maxWidth: "480px", textAlign: "center" } }, e.message),
        h("button.btn.sm", { onclick: () => this.load() }, "Try again"));
      return;
    }
    this.setModel(gltf.scene);
    this.loading.style.display = "none";
  }

  empty() {
    this.loading.style.display = "flex";
    clear(this.loading).appendChild(h("div", { style: { textAlign: "center", maxWidth: "420px", display: "flex", flexDirection: "column", alignItems: "center", gap: "8px" } },
      h("div.eicon", { style: { width: "46px", height: "46px", borderRadius: "13px", background: "rgba(255,255,255,.06)", display: "flex", alignItems: "center", justifyContent: "center" } }, icon("box", 22)),
      h("h3", { style: { margin: 0, color: "#ececee" } }, "No board yet"), h("p", { style: { margin: 0 } }, "The 3D view appears once the board exists.")));
  }

  setModel(root) {
    const THREE = this.T;
    const keepCam = !!this.model;
    if (this.model) { this.select(null, true); this.scene.remove(this.model); dispose(this.model); }
    this.model = root;
    this.partNodes = [];
    this.maskMats = [];
    const refs = this.knownRefs();
    root.traverse((o) => {
      if (o !== root && o.name && (refs ? refs.has(o.name) : /^[A-Z]{1,4}\d+[A-Z]?$/.test(o.name)) && !this.partNodes.some((p) => isAncestor(p, o))) this.partNodes.push(o);
      if (o.isMesh) {
        const mats = Array.isArray(o.material) ? o.material : [o.material];
        for (const m of mats) {
          if (m.transparent && m.opacity > 0.6 && m.opacity < 0.95 && m.color && m.color.g > m.color.r && m.color.g > 0.12 && !this.maskMats.includes(m)) {
            m.userData.baseColor = m.color.clone();
            this.maskMats.push(m);
          }
        }
      }
    });
    this.scene.add(root);
    const board = new THREE.Box3();
    this.bodies = [];
    root.traverse((o) => {
      if (!o.isMesh || this.partNodes.some((p) => isAncestor(p, o))) return;
      board.expandByObject(o);
      for (let x = o; x && x !== root; x = x.parent) if (/_PCB$/i.test(x.name || "")) { this.bodies.push(o); break; }
    });
    this.boardBox = board.isEmpty() ? new THREE.Box3().setFromObject(root) : board;
    this.box = new THREE.Box3().setFromObject(root);
    root.updateMatrixWorld(true);
    this.partBoxes = this.partNodes.map((p) => ({ p, box: new THREE.Box3().setFromObject(p) }));
    root.traverse((o) => { if (o.isMesh) { o.matrixAutoUpdate = false; o.updateMatrix(); } });
    this.setParts(this.parts);
    if (this.mask) this.setMask(this.mask);
    if (!keepCam) this.view("iso", true);
    this.flagLayer.render();
    if (this.pendingProbe) { const r = this.pendingProbe; this.pendingProbe = null; this.probe(r); }
    this.dirty();
  }

  boardData() { return (this.ws.views.board && this.ws.views.board.data) || this.board || null; }

  knownRefs() {
    const b = this.boardData();
    return b ? new Set(b.footprints.map((f) => f.ref)) : null;
  }

  partInfo(ref) {
    const b = this.boardData();
    return b ? b.footprints.find((f) => f.ref === ref) : null;
  }

  // ------------------------------------------------------------------ camera
  view(k, instant) {
    if (!this.box) return;
    this.lastView = k;
    const THREE = this.T;
    const [theta, phi] = VIEWS[k] || VIEWS.iso;
    const center = this.boardBox.getCenter(new THREE.Vector3());
    const size = this.box.getSize(new THREE.Vector3());
    const span = Math.max(size.x, size.z, 0.005);
    const aspect = this.camera.aspect || 1.6;
    const fov = this.camera.fov * Math.PI / 180;
    let r = (span / 2) / Math.tan(fov / 2) * 1.08;
    if (aspect < 1) r /= aspect;
    if (k === "top" || k === "bottom") r = Math.max(size.x / aspect, size.z) / 2 / Math.tan(fov / 2) * 1.15;
    const to = { target: center, pos: new THREE.Vector3(r * Math.sin(phi) * Math.sin(theta), r * Math.cos(phi), r * Math.sin(phi) * Math.cos(theta)).add(center) };
    if (instant) { this.camera.position.copy(to.pos); this.controls.target.copy(to.target); this.controls.update(); this.dirty(); return; }
    const from = { target: this.controls.target.clone(), pos: this.camera.position.clone() };
    const t0 = performance.now();
    this.anim = (now) => {
      const t = Math.min(1, (now - t0) / 450), e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      this.controls.target.lerpVectors(from.target, to.target, e);
      const a = from.pos.clone().sub(from.target), b = to.pos.clone().sub(to.target);
      const sa = new THREE.Spherical().setFromVector3(a), sb = new THREE.Spherical().setFromVector3(b);
      let dth = sb.theta - sa.theta;
      if (dth > Math.PI) dth -= 2 * Math.PI; if (dth < -Math.PI) dth += 2 * Math.PI;
      const s = new THREE.Spherical(sa.radius + (sb.radius - sa.radius) * e, sa.phi + (sb.phi - sa.phi) * e, sa.theta + dth * e);
      this.camera.position.copy(this.controls.target).add(new THREE.Vector3().setFromSpherical(s));
      this.controls.update();
      if (t >= 1) this.anim = null;
    };
    this.dirty();
  }

  resize() {
    if (!this.renderer) return;
    const r = this.stage.getBoundingClientRect();
    if (!r.width || !r.height) return;
    this.renderer.setSize(r.width, r.height, false);
    this.canvas.style.width = "100%"; this.canvas.style.height = "100%";
    this.camera.aspect = r.width / r.height;
    this.camera.updateProjectionMatrix();
    this.dirty();
  }

  dirty() { this.needs = true; }

  // While the view moves, draw at a lighter resolution; sharpen once it rests.
  moving(on) {
    clearTimeout(this.dprT);
    const low = Math.min(this.maxDpr, 1.25);
    if (on) { if (this.dpr !== low) this.setDpr(low); }
    else this.dprT = setTimeout(() => { if (!this.dragging && !this.anim) this.setDpr(this.maxDpr); }, 220);
  }

  setDpr(v) {
    if (!this.renderer || this.dpr === v) return;
    this.dpr = v;
    this.renderer.setPixelRatio(v);
    this.resize();
  }

  loop() {
    let wasAnim = false;
    const tick = (now) => {
      this.raf = requestAnimationFrame(tick);
      if (!this.el.classList.contains("on")) return;
      if (this.anim) { if (!wasAnim) this.moving(true); wasAnim = true; this.anim(now); this.needs = true; }
      else if (wasAnim) { wasAnim = false; this.moving(false); }
      const moved = this.controls.update();
      if (!this.needs && !moved) return;
      this.needs = false;
      this.renderer.render(this.scene, this.camera);
      this.flagLayer.update();
    };
    this.raf = requestAnimationFrame(tick);
  }

  // board mm (+ the 3D point, if the flag has one) -> screen px, or null when behind the camera
  project(x, y, w) {
    if (!this.boardBox || !this.T) return null;
    const THREE = this.T;
    const p = w && w.p3 ? new THREE.Vector3(...w.p3) : new THREE.Vector3(x / 1000, this.boardBox.max.y, y / 1000);
    const v = p.project(this.camera);
    if (v.z > 1 || v.z < -1) return null;
    const r = this.stage.getBoundingClientRect();
    return [(v.x + 1) / 2 * r.width, (1 - v.y) / 2 * r.height];
  }

  // What is under a screen point: the nearest part (its box first, then its triangles), else the board
  // body. Never the whole model: a big board's copper alone is hundreds of thousands of triangles.
  hit(px, py) {
    if (!this.model) return null;
    const THREE = this.T;
    const r = this.canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(px / r.width * 2 - 1, -(py / r.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const ray = this.raycaster.ray;
    let board = null;
    if (this.bodies && this.bodies.length) {
      const b = this.raycaster.intersectObjects(this.bodies, false);
      if (b.length) board = b[0];
    } else {
      const q = ray.intersectBox(this.boardBox, new THREE.Vector3());
      if (q) board = { point: q, distance: q.distanceTo(ray.origin) };
    }
    let best = null;
    if (this.parts && this.partBoxes) {
      const tmp = new THREE.Vector3(), cands = [];
      for (const pb of this.partBoxes) {
        if (!pb.p.visible) continue;
        const q = ray.intersectBox(pb.box, tmp);
        if (q) cands.push([q.distanceTo(ray.origin), pb.p]);
      }
      cands.sort((a, b) => a[0] - b[0]);
      for (const [d, p] of cands) {
        if (best && d > best.distance) break;
        if (board && d > board.distance + 0.0005) break;
        const hs = this.raycaster.intersectObject(p, true).filter((i) => i.object.visible && visibleUp(i.object));
        if (hs.length && (!best || hs[0].distance < best.distance)) best = { point: hs[0].point, distance: hs[0].distance, part: p };
      }
    }
    if (best && (!board || best.distance <= board.distance + 0.0002)) return best;
    return board ? { point: board.point, part: null } : null;
  }

  pickWorld(px, py) {
    const hit = this.hit(px, py);
    if (!hit) return null;
    const p = hit.point;
    const extra = { p3: [+(p.x).toFixed(5), +(p.y).toFixed(5), +(p.z).toFixed(5)], side: p.y < (this.boardBox.min.y + this.boardBox.max.y) / 2 ? "B" : "F" };
    if (hit.part) extra.refs = [hit.part.name];
    return [p.x * 1000, p.z * 1000, extra];
  }

  pointer() {
    const c = this.canvas;
    let down = null;
    c.addEventListener("mousedown", (e) => {
      if (this.flags.down(e)) return;
      down = { x: e.clientX, y: e.clientY };
    });
    c.addEventListener("mouseup", (e) => {
      if (!down || this.flags.active) return;
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4;
      down = null;
      if (moved || e.button !== 0) return;
      const r = c.getBoundingClientRect();
      const hit = this.hit(e.clientX - r.left, e.clientY - r.top);
      this.select(hit && hit.part ? hit.part : null);
    });
    c.addEventListener("dblclick", (e) => {
      const r = c.getBoundingClientRect();
      const hit = this.hit(e.clientX - r.left, e.clientY - r.top);
      if (!hit) return;
      const THREE = this.T;
      const target = hit.part ? new THREE.Box3().setFromObject(hit.part).getCenter(new THREE.Vector3()) : hit.point.clone();
      const from = { t: this.controls.target.clone(), p: this.camera.position.clone() };
      const dist = this.camera.position.distanceTo(this.controls.target) * 0.45;
      const dir = this.camera.position.clone().sub(this.controls.target).normalize();
      const t0 = performance.now();
      this.anim = (now) => {
        const t = Math.min(1, (now - t0) / 420), k = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
        this.controls.target.lerpVectors(from.t, target, k);
        this.camera.position.lerpVectors(from.p, target.clone().add(dir.clone().multiplyScalar(dist)), k);
        if (t >= 1) this.anim = null;
      };
    });
    let last = 0;
    c.addEventListener("mousemove", (e) => {
      if (this.dragging || this.flags.active || !this.model) { this.tip.style.display = "none"; return; }
      const now = performance.now();
      if (now - last < 70) return;
      last = now;
      const r = c.getBoundingClientRect();
      const px = e.clientX - r.left, py = e.clientY - r.top;
      const hit = this.hit(px, py);
      if (!hit || !hit.part) { this.tip.style.display = "none"; c.style.cursor = ""; return; }
      const f = this.partInfo(hit.part.name);
      this.tip.innerHTML = `<b>${esc(hit.part.name)}</b> ${esc(f ? f.val : "")}${f ? `<br><span class="k">${esc(f.lib.split(":").pop())}</span>` : ""}`;
      this.tip.style.display = "block";
      this.tip.style.left = Math.min(px + 16, r.width - 260) + "px";
      this.tip.style.top = Math.min(py + 14, r.height - 70) + "px";
      c.style.cursor = "pointer";
    });
    c.addEventListener("mouseleave", () => { this.tip.style.display = "none"; });
  }

  // ------------------------------------------------------------------ parts, mask, selection
  setParts(on) {
    this.parts = on;
    this.partsBtn.classList.toggle("on", on);
    for (const p of this.partNodes || []) p.visible = on;
    this.dirty();
  }

  maskMenu(anchor) {
    menu(anchor, [{ head: "Solder mask" }, { label: "As designed", checked: !this.mask, run: () => this.setMask(null) },
      ...MASKS.map(([k, label]) => ({ label, checked: this.mask === k, run: () => this.setMask(k) })),
      "-", { label: this.showMask ? "Hide the mask (see the copper)" : "Show the mask", icon: this.showMask ? "eye-off" : "eye", run: () => this.toggleMask() }], { align: "end" });
  }

  setMask(k) {
    this.mask = k;
    const m = MASKS.find((x) => x[0] === k);
    for (const mat of this.maskMats || []) {
      if (m) mat.color.setHex(m[2]); else if (mat.userData.baseColor) mat.color.copy(mat.userData.baseColor);
      if (mat.userData.baseRough === undefined) mat.userData.baseRough = mat.roughness;
      mat.roughness = k === "matte-black" ? 0.92 : mat.userData.baseRough;
      mat.needsUpdate = true;
    }
    this.dirty();
  }

  toggleMask() {
    this.showMask = !this.showMask;
    for (const mat of this.maskMats || []) { mat.visible = this.showMask; mat.needsUpdate = true; }
    this.dirty();
  }

  // parts picked in another view: select the first of them here
  probe(refs) {
    if (!this.partNodes) { this.pendingProbe = refs; return; }
    const p = refs && refs.length ? this.partNodes.find((n) => n.name === refs[0]) : null;
    this.select(p || null, true);
  }

  select(part, quiet) {
    const had = !!this.selected;
    if (this.selected) for (const [o, m] of this.selected.saved) o.material = m;
    if (this.selBox) { this.scene.remove(this.selBox); this.selBox.geometry.dispose(); this.selBox = null; }
    this.selected = null;
    clear(this.info);
    this.info.style.display = "none";
    if (part) {
      const saved = [];
      part.traverse((o) => {
        if (!o.isMesh) return;
        saved.push([o, o.material]);
        const hl = (m) => { const c = m.clone(); if (c.emissive) { c.emissive = new this.T.Color(0xeb8a50); c.emissiveIntensity = 0.45; } return c; };
        o.material = Array.isArray(o.material) ? o.material.map(hl) : hl(o.material);
      });
      this.selected = { part, saved };
      this.selBox = new this.T.BoxHelper(part, 0xf4a574);
      this.scene.add(this.selBox);
      const f = this.partInfo(part.name);
      this.info.style.display = "flex";
      this.info.append(icon("microchip", 14), h("span.sl", `${part.name}${f ? "  " + f.val : ""}`),
        f ? h("span", { style: { color: "var(--hud-muted)", fontSize: "11.5px" } }, f.lib.split(":").pop()) : null,
        h("button.tbtn", { onclick: () => this.ws.locate({ ref: part.name }), "data-tip": "Show on board" }, icon("circuit-board", 14)),
        h("button.tbtn", { onclick: () => { this.ws.show("bom"); this.ws.view("bom").probe([part.name], "3d"); }, "data-tip": "Show it in the BOM" }, icon("list", 14)),
        h("button.tbtn", { "data-tip": "Flag it", onclick: () => {
          const THREE = this.T;
          const c = new THREE.Box3().setFromObject(part).getCenter(new THREE.Vector3());
          this.flags.create({ x: c.x * 1000, y: c.z * 1000, p3: [c.x, c.y, c.z], refs: [part.name] });
        } }, icon("flag", 14)),
        h("button.tbtn", { onclick: () => this.select(null), "data-tip": "Clear" }, icon("x", 14)));
      if (!quiet) this.ws.select([{ ref: part.name, kind: "footprint" }], "3d");
    } else if (had && !quiet) this.ws.select([], "3d");
    this.dirty();
  }

  focusFlag(f) {
    const go = () => {
      if (!this.boardBox) return;
      const w = f.where || {};
      const THREE = this.T;
      const target = w.p3 ? new THREE.Vector3(...w.p3) : new THREE.Vector3(w.x / 1000, this.boardBox.max.y, w.y / 1000);
      const dir = this.camera.position.clone().sub(this.controls.target).normalize();
      const dist = Math.max(0.02, this.box.getSize(new THREE.Vector3()).length() * 0.35);
      const from = { t: this.controls.target.clone(), p: this.camera.position.clone() };
      const t0 = performance.now();
      this.anim = (now) => {
        const t = Math.min(1, (now - t0) / 450), k = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
        this.controls.target.lerpVectors(from.t, target, k);
        this.camera.position.lerpVectors(from.p, target.clone().add(dir.clone().multiplyScalar(dist)), k);
        if (t >= 1) this.anim = null;
      };
      this.flagLayer.mark(f.id);
      setTimeout(() => this.flagLayer.mark(null), 1600);
    };
    if (this.boardBox) go(); else setTimeout(go, 1500);
  }

  // ------------------------------------------------------------------ pictures
  capture(w) {
    this.renderer.render(this.scene, this.camera);
    const src = this.canvas;
    const W = Math.min(1100, src.width), H = Math.round(W * src.height / src.width);
    const c = document.createElement("canvas");
    c.width = W; c.height = H;
    const g = c.getContext("2d");
    g.drawImage(src, 0, 0, W, H);
    if (w) {
      const p = this.project(w.x, w.y, w);
      const r = this.stage.getBoundingClientRect();
      if (p) {
        const sx = p[0] / r.width * W, sy = p[1] / r.height * H;
        g.strokeStyle = "rgba(255,170,110,1)"; g.lineWidth = 3;
        g.beginPath(); g.arc(sx, sy, 16, 0, Math.PI * 2); g.stroke();
      }
    }
    return c;
  }

  async snapshot(w) { return this.capture(w).toDataURL("image/png"); }

  async savePicture() {
    if (!this.renderer) return;
    const blob = await new Promise((res) => this.capture(null).toBlob(res, "image/png"));
    const name = `3d-view-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.png`;
    try {
      const r = await upload(`/api/projects/${enc(this.pid)}/upload`, [new File([blob], name, { type: "image/png" })], { dir: "build/images" });
      toast(`Saved ${r.saved[0]}`, "ok", 5000, { label: "Show", run: () => { this.ws.show("files"); this.ws.view("files").load("build/images"); } });
    } catch (e) { toast(e.message, "error"); }
  }

  // ------------------------------------------------------------------ the raytraced renders (kicad-cli pcb render)
  renders(on, render) {
    for (const b of this.modeSeg.children) b.classList.toggle("on", b.dataset.mode === (on ? "renders" : "model"));
    this.gallery.style.display = on ? "block" : "none";
    if (!on) { this.dirty(); return; }
    if (render) this.renderNow();
    this.drawGallery();
  }

  async renderNow() {
    this.busy = true; this.drawGallery();
    toast("Rendering…");
    try { await api(`/api/projects/${enc(this.pid)}/render3d`, { body: {} }); } catch (e) { toast(e.message, "error"); }
    this.busy = false; this.drawGallery();
  }

  async drawGallery() {
    const d = await api(`/api/projects/${enc(this.pid)}/outputs`).catch(() => ({}));
    const imgs = (d.images || []).filter((f) => /\.(png|jpe?g)$/i.test(f.name));
    const b = clear(this.gallery);
    const inner = h("div.panel-inner", { style: { paddingTop: "64px" } });
    b.appendChild(inner);
    inner.appendChild(h("div.checks-head", h("div.verdict", h("div.vic", icon("camera", 20)), h("div", h("div.vt", "Renders"),
      h("div.vs", "Raytraced by KiCad"))),
      h("div.grow"), this.busy ? h("span.spinner") : null,
      h("button.btn.primary", { disabled: this.busy, onclick: () => this.renderNow() }, icon("camera", 14), h("span", this.busy ? "Rendering…" : "Render"))));
    if (!imgs.length) { inner.appendChild(h("div.empty", h("div.eicon", icon("image", 22)), h("h3", "No renders yet"))); return; }
    inner.appendChild(h("div.gallery", imgs.map((f) => {
      const src = `/api/projects/${enc(this.pid)}/file?path=${enc(f.path)}&raw=1&t=${f.mtime}`;
      return h("figure", h("img", { src, onclick: () => lightbox(src) }), h("figcaption", `${f.name} · ${fmtTime(f.mtime)}`));
    })));
  }
}

function isAncestor(a, o) { for (let x = o; x; x = x.parent) if (x === a) return true; return false; }
function visibleUp(o) { for (let x = o; x; x = x.parent) if (!x.visible) return false; return true; }
function dispose(root) {
  root.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    const mats = o.material ? (Array.isArray(o.material) ? o.material : [o.material]) : [];
    for (const m of mats) { for (const k in m) if (m[k] && m[k].isTexture) m[k].dispose(); m.dispose(); }
  });
}
function esc(s) { return String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
