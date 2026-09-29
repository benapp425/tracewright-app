"""A firmware starter from the schematic: which net each microcontroller pin carries, what is on it, and a
bring-up sketch that exercises the board.

  firmware/pins.h               the pin map as C constants (named from the nets), with what each pin drives
  firmware/PINS.md              the same as a table
  firmware/bringup/bringup.ino  an Arduino sketch: LEDs lit in turn, I2C buses scanned, buttons and analog
                                inputs read, reported on the serial port (or blinked, on a board without one)

pins.h and PINS.md say they are generated and are written again each time (`./tw firmware`); the sketch is
written only when there is none, so edits to it stay. Pin constants use the names of the Arduino core for the
family (ATTinyCore / MiniCore PIN_PB1, RP2040 and ESP32 GPIO numbers, STM32duino PB1, nRF52 numbers); for a
family whose core naming is not known here the pins are listed in comments, not guessed."""
import datetime, os, re

PORT = re.compile(r"^(P[A-K]\d{1,2}|GPIO\d{1,2}|IO\d{1,2}|P\d\.\d{1,2})$", re.I)
FAMILIES = [("attiny", r"ATTINY\d", "ATTinyCore"), ("atmega", r"ATMEGA\d", "MiniCore / MegaCore"), ("rp2040", r"RP2040|RP2350", "Arduino-Pico"),
            ("esp32", r"ESP32", "Arduino-ESP32"), ("stm32", r"STM32", "STM32duino"), ("nrf52", r"NRF52", "Adafruit nRF52"),
            ("samd", r"ATSAMD|SAMD\d", None), ("ch32", r"CH32V", None), ("pic", r"PIC\d{2}", None), ("msp430", r"MSP430", None)]


def family(value, lib=""):
    s = f"{value} {lib}".upper()
    for fam, pat, core in FAMILIES:
        if re.search(pat, s):
            return fam, core
    return None, None


def port_of(pin_name):
    """The port pin in a pin's name: "PB0/MOSI/SDA" -> PB0, "~{RESET}/PB5" -> PB5, "GPIO4" -> GPIO4."""
    for tok in re.split(r"[/_\s,()]+", re.sub(r"~\{([^}]*)\}", r"\1", pin_name or "")):
        if PORT.match(tok):
            return tok.upper()
    return None


def arduino_name(fam, port):
    """The pin's name in the family's Arduino core, or None when it is not known here."""
    if not port:
        return None
    if fam in ("attiny", "atmega"):
        m = re.match(r"P([A-K])(\d)$", port)
        return f"PIN_P{m.group(1)}{m.group(2)}" if m else None
    if fam in ("rp2040", "esp32"):
        m = re.match(r"(?:GPIO|IO)(\d+)$", port)
        return m.group(1) if m else None
    if fam == "stm32":
        return port if re.match(r"P[A-K]\d{1,2}$", port) else None
    if fam == "nrf52":
        m = re.match(r"P(\d)\.(\d+)$", port)
        return str(int(m.group(1)) * 32 + int(m.group(2))) if m else None
    return None


def find_mcus(nl):
    """Parts that look like microcontrollers: a known family, or at least three port-named pins."""
    out = []
    for ref, part in sorted(nl.parts.items()):
        pins = nl.pins_of(ref)
        if len(pins) < 6:
            continue
        fam, _ = family(part.get("value", ""), part.get("lib", "") + " " + part.get("footprint", ""))
        ports = sum(1 for p in pins if port_of(nl.pin_name(ref, p)))
        if fam or ports >= 3:
            out.append(ref)
    return out


def _others(nl, net, ref, pin):
    return [(r, p) for r, p in nl.nets.get(net, []) if not (r == ref and p == pin)]


def _is_led(nl, ref):
    part = nl.parts.get(ref) or {}
    s = f"{ref} {part.get('value', '')} {part.get('lib', '')} {part.get('footprint', '')}".upper()
    return ref.upper().startswith(("D", "LED")) and "LED" in s


def _led_polarity(nl, led, pin):
    """HIGH or LOW lights an LED whose pin `pin` is on the MCU's side."""
    n = (nl.pin_name(led, pin) or "").upper()
    if n in ("A", "ANODE", "+") or n.startswith("A"):
        return "HIGH"
    if n in ("K", "C", "CATHODE", "-") or n.startswith(("K", "C")):
        return "LOW"
    return None


def role(nl, ref, pin, net):
    """What a pin does on this board: (role, detail, extra) -- led, button, i2c, uart, spi, usb, debug, analog
    or gpio."""
    short = net.split("/")[-1].upper()
    others = _others(nl, net, ref, pin)
    if re.search(r"(^|_)SDA\d?$|(^|_)SCL\d?$", short):
        return "i2c", "SDA" if "SDA" in short else "SCL", None
    if re.search(r"(^|_)(TX|TXD|RX|RXD)\d?$|UART.*(TX|RX)", short):
        return "uart", "TX" if "TX" in short else "RX", None
    if re.search(r"(^|_)(MOSI|MISO|SCK|SCLK|CS|NSS|SS)\d?$|SPI", short):
        return "spi", short, None
    if re.search(r"USB|D[+-]$|DP$|DM$", short):
        return "usb", short, None
    if re.search(r"SWDIO|SWCLK|SWO|RESET|NRST|UPDI|JTAG", short):
        return "debug", short, None
    for r, p in others:
        if _is_led(nl, r):
            return "led", r, _led_polarity(nl, r, p)
        if r.upper().startswith(("SW", "BTN", "S")) and len(nl.pins_of(r)) <= 6 and not r.upper().startswith("SH"):
            return "button", r, None
        if r.upper().startswith("R") and len(nl.pins_of(r)) == 2:                 # through a resistor to an LED
            other = next((q for q in nl.pins_of(r) if q != p), None)
            far = nl.pin.get((r, str(other))) if other else None
            for r2, p2 in nl.nets.get(far, []) if far else []:
                if _is_led(nl, r2):
                    return "led", r2, _led_polarity(nl, r2, p2)
    if re.search(r"ADC|AIN|SENSE|VBAT|VMEAS|THERM|NTC|POT", short):
        return "analog", short, None
    return "gpio", short, None


def build(nl, project_name=""):
    """The pin map of every microcontroller: [{ref, value, family, core, pins: [...]}]."""
    out = []
    for ref in find_mcus(nl):
        part = nl.parts[ref]
        fam, core = family(part.get("value", ""), part.get("lib", "") + " " + part.get("footprint", ""))
        pins = []
        for pin in nl.pins_of(ref):
            net = nl.pin.get((ref, str(pin)))                  # the full name (the netlist's nets are keyed by it)
            name = nl.pin_name(ref, pin)
            if not net or net.startswith("unconnected-") or re.match(r"^(GND|VSS|VCC|VDD|AVCC|AVDD|VBUS|NC$|\+?\d)", net.split("/")[-1].upper()):
                continue
            port = port_of(name)
            if not port:
                continue
            what, detail, extra = role(nl, ref, pin, net)
            to = []
            for r, p in _others(nl, net, ref, pin)[:5]:
                v = (nl.parts.get(r) or {}).get("value", "")
                to.append(f"{r}.{p}" + (f" ({v})" if v and not r.upper().startswith(("J", "P", "TP")) else ""))
            pins.append({"pin": pin, "name": name, "port": port, "net": net, "short": net.split("/")[-1], "role": what,
                         "detail": detail, "on": extra, "to": to, "arduino": arduino_name(fam, port)})
        out.append({"ref": ref, "value": part.get("value", ""), "family": fam, "core": core, "pins": pins})
    return out


def _const(short, port=""):
    """PIN_<the net's name>, without a port prefix the net's name already carries (PB3_USB_N -> PIN_USB_N)."""
    c = re.sub(r"[^A-Za-z0-9]+", "_", short).strip("_").upper()
    if port and c.startswith(port.upper() + "_") and len(c) > len(port) + 1:
        c = c[len(port) + 1:]
    return "PIN_" + (c if not c[:1].isdigit() else "N" + c)


def header(mcu, board_name=""):
    lines = [f"// pins.h -- which net each pin of {mcu['ref']} ({mcu['value']}) carries, from the schematic of {board_name or 'this board'}.",
             f"// Generated by Tracewright on {datetime.date.today().isoformat()}; `./tw firmware` writes it again, so change the",
             "// schematic rather than this file.",
             f"// Pin names are {mcu['core']}'s." if mcu["core"] else
             "// The Arduino core's pin numbers for this family are not known here: each pin is listed with its port name;"]
    if not mcu["core"]:
        lines.append("// define the numbers your core uses.")
    lines += ["#pragma once", ""]
    groups = [("i2c", "I2C"), ("uart", "UART"), ("spi", "SPI"), ("led", "LEDs"), ("button", "Buttons"), ("analog", "Analog inputs"),
              ("usb", "USB"), ("debug", "Programming and debug"), ("gpio", "Other signals")]
    seen = set()
    for key, title in groups:
        ps = [p for p in mcu["pins"] if p["role"] == key]
        if not ps:
            continue
        lines.append(f"// {title}")
        for p in ps:
            c = _const(p["short"], p["port"])
            while c in seen:
                c += "_"
            seen.add(c)
            val = p["arduino"]
            note = f"pin {p['pin']} ({p['port']})" + (f": to {', '.join(p['to'])}" if p["to"] else "")
            lines.append(f"#define {c:<22} {val:<8} // {note}" if val else f"// #define {c:<19} ?        // {note}")
            if key == "led" and p["on"] and val:
                lines.append(f"#define {c + '_ON':<22} {p['on']:<8} // {p['detail']} lights on {p['on']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def table(mcus, board_name=""):
    out = [f"# Pin map: {board_name}" if board_name else "# Pin map", "",
           "_Generated from the schematic by `./tw firmware`; change the schematic, not this file._", ""]
    for m in mcus:
        out += [f"## {m['ref']} ({m['value']})", "", "| Pin | Port | Net | Does | Wired to |", "|---|---|---|---|---|"]
        for p in m["pins"]:
            does = {"led": f"LED {p['detail']}" + (f", {p['on']} lights it" if p["on"] else ""), "button": f"button {p['detail']}",
                    "i2c": f"I2C {p['detail']}", "uart": f"UART {p['detail']}"}.get(p["role"], p["role"])
            out.append(f"| {p['pin']} | {p['port']} | {p['short']} | {does} | {', '.join(p['to'])} |")
        out.append("")
    return "\n".join(out)


def sketch(mcu, board_name=""):
    """The bring-up sketch for the first microcontroller (only the pins the core has names for)."""
    usable = [p for p in mcu["pins"] if p["arduino"]]
    leds = [p for p in usable if p["role"] == "led"]
    buttons = [p for p in usable if p["role"] == "button"]
    analog = [p for p in usable if p["role"] == "analog"]
    i2c = any(p["role"] == "i2c" for p in usable)
    uart = any(p["role"] == "uart" for p in usable) or mcu["family"] in ("rp2040", "esp32", "stm32", "nrf52")
    c = lambda p: _const(p["short"], p["port"])
    L = [f"// Bring-up for {board_name or 'the board'} ({mcu['ref']}, {mcu['value']}): each part of the board in turn.",
         f"// Written once by Tracewright (`./tw firmware`); yours to change. Build it with the {mcu['core'] or 'Arduino'} core.",
         "// " + ("Results go to the serial port at 115200 baud." if uart else "No serial port: counts are blinked on the first LED."),
         '#include "pins.h"']
    if i2c:
        L.append("#include <Wire.h>")
    L += ["", "static void report(const char *what, int n) {"]
    if uart:
        L += ["  Serial.print(what); Serial.print(\": \"); Serial.println(n);"]
    elif leds:
        on = f"{c(leds[0])}_ON" if leds[0]["on"] else "HIGH"
        L += [f"  delay(600);", f"  for (int i = 0; i < n; i++) {{ digitalWrite({c(leds[0])}, {on}); delay(250); digitalWrite({c(leds[0])}, !{on}); delay(250); }}"]
    else:
        L += ["  (void)what; (void)n;"]
    L += ["}", "", "void setup() {"]
    if uart:
        L += ["  Serial.begin(115200);", "  delay(1500);", f'  Serial.println("{board_name or "board"} bring-up");']
    for p in leds:
        on = f"{c(p)}_ON" if p["on"] else "HIGH"
        L += [f"  pinMode({c(p)}, OUTPUT);  // LED {p['detail']}", f"  digitalWrite({c(p)}, {on}); delay(800); digitalWrite({c(p)}, !{on});"]
    for p in buttons:
        L += [f"  pinMode({c(p)}, INPUT_PULLUP);  // button {p['detail']}"]
    if i2c:
        L += ["  Wire.begin();", "  int found = 0;", "  for (uint8_t a = 1; a < 127; a++) {",
              "    Wire.beginTransmission(a);", "    if (Wire.endTransmission() == 0) {", "      found++;"]
        if uart:
            L += ['      Serial.print("I2C device at 0x"); Serial.println(a, HEX);']
        L += ["    }", "  }", '  report("I2C devices", found);']
    L += ["}", "", "void loop() {"]
    for p in buttons:
        L += [f'  if (digitalRead({c(p)}) == LOW) report("button {p["detail"]}", 1);']
    for p in analog:
        L += [f'  report("{p["short"]}", analogRead({c(p)}));']
    if leds:
        on = f"{c(leds[0])}_ON" if leds[0]["on"] else "HIGH"
        L += [f"  digitalWrite({c(leds[0])}, {on}); delay(100); digitalWrite({c(leds[0])}, !{on});  // alive"]
    L += ["  delay(1000);", "}"]
    return "\n".join(L) + "\n"


def write(project, nl, board_name=""):
    """Write the starter; returns the paths written (relative to the project)."""
    mcus = build(nl, board_name)
    if not mcus:
        return []
    d = os.path.join(project.root, "firmware")
    os.makedirs(os.path.join(d, "bringup"), exist_ok=True)
    out = []
    for i, m in enumerate(mcus):
        name = "pins.h" if i == 0 else f"pins_{m['ref'].lower()}.h"
        text = header(m, board_name)
        for f in (os.path.join(d, name), os.path.join(d, "bringup", name)) if i == 0 else (os.path.join(d, name),):
            with open(f, "w") as fh:
                fh.write(text)
            out.append(os.path.relpath(f, project.root))
    with open(os.path.join(d, "PINS.md"), "w") as fh:
        fh.write(table(mcus, board_name))
    out.append("firmware/PINS.md")
    ino = os.path.join(d, "bringup", "bringup.ino")
    if not os.path.exists(ino):
        with open(ino, "w") as fh:
            fh.write(sketch(mcus[0], board_name))
        out.append(os.path.relpath(ino, project.root))
    return out
