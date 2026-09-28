// Geometry checks for the guided start's block diagram (tracewright/web/js/blockdiagram.js), run by
// tests/run_tests.py when Node is installed:  node tests/diagram_check.mjs
// Lays out real and awkward diagrams at three column widths and checks that no wire crosses a block or
// someone else's label, no two wires run on top of each other, labels do not collide, and everything
// is inside the frame.
import { layoutDiagram, diagramSVG } from "../tracewright/web/js/blockdiagram.js";

const B = (id, kind, label, note) => ({ id, kind, label: label || id, note: note || "" });
const L = (from, to, label, kind) => ({ from, to, label: label || "", kind: kind || "signal" });

const CASES = {
  // What Claude drew for the desk air monitor (labels with line breaks, a GND link doubling the 5V one).
  desk: {
    blocks: [B("USB", "connector", "USB-C 5V", "Power input, 0.5-1A"), B("REG", "power", "5V → 3.3V\nAMS1117-3.3", "LDO regulator, 1A"),
      B("MCU", "mcu", "ESP32-C3-WROOM", "Wi-Fi, I²C, GPIO"), B("SCD41", "sensor", "SCD41\nCO₂/T/H", "Sensirion, I²C"),
      B("OLED", "display", "OLED 0.96\"\n128×64", "I²C module, pre-assembled"), B("DECAP", "other", "Decoupling\nCapacitors", "100nF on 3.3V, input caps on 5V")],
    links: [L("USB", "REG", "5V", "power"), L("REG", "MCU", "3.3V", "power"), L("REG", "SCD41", "3.3V", "power"), L("REG", "OLED", "3.3V", "power"),
      L("MCU", "SCD41", "I²C (SDA/SCL)"), L("MCU", "OLED", "I²C (SDA/SCL)"), L("USB", "REG", "GND", "power")],
  },
  // A busier board: USB data skipping the power row, a header's UART, flash beside the MCU, a bus to
  // three sensors, a display on SPI, LEDs, a charger feeding the regulator in the same row.
  logger: {
    blocks: [B("J1", "connector", "USB-C", "5V in, USB 2.0"), B("J2", "connector", "Debug header", "SWD + UART"),
      B("BAT", "connector", "LiPo JST-PH"), B("CHG", "power", "Charger", "MCP73831, 500 mA"), B("LDO", "power", "LDO 3.3V", "AP2112K"),
      B("MCU", "mcu", "RP2040", "Dual M0+, 133 MHz"), B("FLASH", "memory", "QSPI flash", "W25Q128, 16 MB"),
      B("IMU", "sensor", "IMU", "LSM6DSO"), B("BARO", "sensor", "Barometer", "BMP581"), B("HUM", "sensor", "Humidity", "SHT40"),
      B("LCD", "display", "TFT 1.14\"", "ST7789, SPI"), B("LED", "io", "Status LEDs", "Red, green")],
    links: [L("J1", "CHG", "5V", "power"), L("BAT", "CHG", "VBAT", "power"), L("CHG", "LDO", "VSYS", "power"), L("LDO", "MCU", "3.3V", "power"),
      L("LDO", "IMU", "3.3V", "power"), L("LDO", "BARO", "3.3V", "power"), L("LDO", "HUM", "3.3V", "power"), L("LDO", "FLASH", "3.3V", "power"),
      L("J1", "MCU", "USB D+/D−"), L("J2", "MCU", "SWD", "bus"), L("J2", "MCU", "UART"), L("MCU", "FLASH", "QSPI", "bus"),
      L("MCU", "IMU", "I²C", "bus"), L("MCU", "BARO", "I²C", "bus"), L("MCU", "HUM", "I²C", "bus"), L("MCU", "LCD", "SPI", "bus"),
      L("MCU", "LED", "GPIO")],
  },
  // Links within one row: neighbours, and a pair with a block between them.
  row: {
    blocks: [B("A", "sensor", "Sensor A"), B("B", "sensor", "Sensor B"), B("C", "sensor", "Sensor C"), B("M", "mcu", "MCU")],
    links: [L("A", "B", "SYNC"), L("A", "C", "TRIG"), L("M", "A", "I²C", "bus"), L("M", "C", "I²C", "bus"), L("M", "B", "INT")],
  },
  // Two wires of different kinds between one pair, and blocks with nothing connected.
  pair: {
    blocks: [B("J", "connector", "Barrel jack", "12 V in"), B("BUCK", "power", "Buck 5V", "TPS54202"), B("X", "other", "Mounting holes"),
      B("Y", "other", "Fiducials"), B("Z", "other", "Test points")],
    links: [L("J", "BUCK", "12V", "power"), L("J", "BUCK", "EN"), L("BUCK", "J", "GND", "power")],
  },
  // Many blocks and links, generated.
  big: (() => {
    const kinds = ["connector", "connector", "power", "power", "power", "mcu", "memory", "rf", "sensor", "sensor", "sensor", "display",
      "io", "io", "motor", "audio", "other", "other"];
    const blocks = kinds.map((k, i) => B(`b${i}`, k, `${k} block ${i} with a long name`, i % 3 ? `note ${i}: some detail about it` : ""));
    const links = [];
    let seed = 7;
    const rnd = () => (seed = (seed * 48271) % 2147483647) / 2147483647;
    for (let i = 0; i < 40; i++) {
      const a = Math.floor(rnd() * kinds.length), b = Math.floor(rnd() * kinds.length);
      links.push(L(`b${a}`, `b${b}`, ["I²C", "SPI", "3.3V", "5V", "GPIO", "UART", ""][i % 7], ["bus", "bus", "power", "power", "signal", "signal", "signal"][i % 7]));
    }
    return { blocks, links };
  })(),
};

const failures = [], hiddenBig = [];
const fail = (name, width, msg) => failures.push(`${name} @${width}: ${msg}`);
const inter = (a, b, e = 0) => a.x + e < b.x + b.w && b.x + e < a.x + a.w && a.y + e < b.y + b.h && b.y + e < a.y + a.h;
const segHitsRect = (s, r, e) => {
  const x0 = Math.min(s.x1, s.x2), x1 = Math.max(s.x1, s.x2), y0 = Math.min(s.y1, s.y2), y1 = Math.max(s.y1, s.y2);
  return x1 > r.x + e && x0 < r.x + r.w - e && y1 > r.y + e && y0 < r.y + r.h - e;
};

for (const [name, d] of Object.entries(CASES)) {
  for (const width of [340, 520, 760]) {
    const out = layoutDiagram(d, { width });
    const rects = out.blocks.map((b) => ({ id: b.id, x: b.x, y: b.y, w: b.w, h: b.h }));
    if (rects.length !== d.blocks.length) fail(name, width, `laid out ${rects.length} of ${d.blocks.length} blocks`);
    for (const r of rects) if ([r.x, r.y, r.h].some((v) => !Number.isFinite(v))) fail(name, width, `block ${r.id} has no position`);
    for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++)
      if (inter(rects[i], rects[j])) fail(name, width, `blocks ${rects[i].id} and ${rects[j].id} overlap`);
    for (const s of out.wires) {
      if (![s.x1, s.y1, s.x2, s.y2].every(Number.isFinite)) { fail(name, width, "a wire has no position"); continue; }
      if (s.x1 !== s.x2 && s.y1 !== s.y2) fail(name, width, "a wire is not horizontal or vertical");
      for (const r of rects) if (segHitsRect(s, r, 1.5)) fail(name, width, `a wire crosses block ${r.id} (${s.x1},${s.y1} → ${s.x2},${s.y2})`);
    }
    for (let i = 0; i < out.wires.length; i++) for (let j = i + 1; j < out.wires.length; j++) {
      const a = out.wires[i], b = out.wires[j];
      if (a.own === b.own) continue;
      const va = a.x1 === a.x2, vb = b.x1 === b.x2;
      if (va && vb && Math.abs(a.x1 - b.x1) < 1) {
        const o = Math.min(Math.max(a.y1, a.y2), Math.max(b.y1, b.y2)) - Math.max(Math.min(a.y1, a.y2), Math.min(b.y1, b.y2));
        if (o > 1) fail(name, width, `two wires run on top of each other at x=${a.x1.toFixed(1)}`);
      }
      if (!va && !vb && Math.abs(a.y1 - b.y1) < 1) {
        const o = Math.min(Math.max(a.x1, a.x2), Math.max(b.x1, b.x2)) - Math.max(Math.min(a.x1, a.x2), Math.min(b.x1, b.x2));
        if (o > 1) fail(name, width, `two wires run on top of each other at y=${a.y1.toFixed(1)}`);
      }
    }
    for (let i = 0; i < out.pills.length; i++) {
      const p = out.pills[i];
      for (const r of rects) if (inter(p, r, 1)) fail(name, width, `label "${p.text}" covers block ${r.id}`);
      for (let j = i + 1; j < out.pills.length; j++) if (inter(p, out.pills[j], 0.5)) fail(name, width, `labels "${p.text}" and "${out.pills[j].text}" overlap`);
      for (const s of out.wires) if (s.own !== p.own && segHitsRect(s, p, 1)) fail(name, width, `a wire runs under label "${p.text}"`);
    }
    const bx = out.box;
    const inside = (x, y) => x >= bx.x - 0.01 && x <= bx.x + bx.w + 0.01 && y >= bx.y - 0.01 && y <= bx.y + bx.h + 0.01;
    for (const r of rects) if (!inside(r.x, r.y) || !inside(r.x + r.w, r.y + r.h)) fail(name, width, `block ${r.id} is outside the frame`);
    for (const p of out.pills) if (!inside(p.x, p.y) || !inside(p.x + p.w, p.y + p.h)) fail(name, width, `label "${p.text}" is outside the frame`);
    if (width <= 520 && bx.w > width * 1.3 && name !== "big") fail(name, width, `drawing is ${Math.round(bx.w)} wide for a ${width} column`);
    if (name !== "big" && out.hidden.length) fail(name, width, `labels with no room: ${out.hidden.join(", ")}`);
    if (name === "big") hiddenBig.push(out.hidden.length);
    const svg = diagramSVG(d, { width });
    if (!svg.startsWith("<svg") || svg.includes("NaN") || svg.includes("undefined")) fail(name, width, "the SVG is malformed");
  }
}

// The desk monitor, specifically: the regulator's 3.3 V reaches the sensor and display as tags, not
// wires through the processor; the I²C bus is one wire with a drop to each; the 5V and GND links are one.
{
  const out = layoutDiagram(CASES.desk, { width: 520 });
  const tags = out.pills.filter((p) => p.tag).map((p) => p.text).sort();
  if (JSON.stringify(tags) !== JSON.stringify(["3.3V", "3.3V"])) failures.push(`desk: supply tags ${JSON.stringify(tags)}`);
  const labels = out.pills.filter((p) => !p.tag).map((p) => p.text).sort();
  if (JSON.stringify(labels) !== JSON.stringify(["3.3V", "5V, GND", "I²C (SDA/SCL)"])) failures.push(`desk: wire labels ${JSON.stringify(labels)}`);
  const titles = Object.fromEntries(out.blocks.map((b) => [b.id, b.title]));
  if (titles.REG.length !== 2 || titles.REG[1] !== "AMS1117-3.3") failures.push(`desk: regulator title ${JSON.stringify(titles.REG)}`);
}

if (failures.length) {
  console.log(failures.slice(0, 40).join("\n") + (failures.length > 40 ? `\n… ${failures.length - 40} more` : ""));
  process.exit(1);
}
console.log(`diagram geometry ok: ${Object.keys(CASES).length} diagrams × 3 widths (stress case: ${hiddenBig.join("/")} labels in tooltips)`);
