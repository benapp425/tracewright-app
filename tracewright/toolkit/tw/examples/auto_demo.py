"""The demo board's schematic laid out by rule (tw.sch.auto): what connects, grouped by function, and
no coordinates. Compare demo_board.py, the same sheets placed by hand.

    python3 -c "from tw.examples.auto_demo import schematic; schematic('hardware/demo')"
    then tw.sch.finish(project): "connections" is "as asked" when KiCad's netlist matches.
"""
from ..sch import Design
from ..sch.auto import Page
from .demo_board import catalog


def schematic(hw_dir, name="demo", libname="Demo_USB-C_ATtiny"):
    cat = catalog()
    d = Design(name, libname=libname, title="USB-C ATtiny85 board", company="Tracewright example design",
               comments=("USB-C sink, AMS1117 3.3 V, ATtiny85, Qwiic I2C",))
    root = d.root("Cover", paper="A4")
    pw = d.sheet("Power", "power.kicad_sch", "USB-C input and 3.3 V", paper="A4",
                 description="USB-C input (5 V sink), 3.3 V regulator, power LED")
    mc = d.sheet("MCU", "mcu.kicad_sch", "ATtiny85 and I2C", paper="A4",
                 description="ATtiny85, USB data lines, Qwiic I2C port")
    root.subsheet(pw, (38.1, 40.64), (50.8, 25.4), [])
    root.subsheet(mc, (132.08, 40.64), (50.8, 25.4), [])
    d.contents((25.4, 86.36))
    d.builder(root, 0, cat).notes((132.08, 86.36), ["Resistors 0402 1 %, capacitors 0402 X7R / X5R, unless noted.",
                                                     "Parts marked DNP are not fitted."])

    p = Page(d, pw, base=0, catalog=cat)
    usb = p.group("USB-C INPUT (5 V SINK)")
    j1 = usb.part("USBC", "J", ref="J1")
    usb.power(j1, "A4", "+5V", flag=True)                       # VBUS, flagged where power enters
    usb.pull(j1, "A5", "R5k1", "GND", net="CC1", ref="R1")      # Rd on each CC pin
    usb.pull(j1, "B5", "R5k1", "GND", net="CC2", ref="R2")
    usb.net(j1, ["A7", "B7"], "USB_D_N")                        # both rows joined at the receptacle
    usb.net(j1, ["A6", "B6"], "USB_D_P")
    usb.nc(j1, ["A8", "B8"])
    usb.power(j1, ["A1", "SH"], "GND")
    usb.note("Rd 5.1k on each CC pin: a USB-C source turns VBUS on.", near="R1")
    reg = p.group("3.3 V REGULATOR")
    u1 = reg.part("LDO", "U", ref="U1")
    reg.decouple(u1, "3", ["C10u"], "+5V", refs=["C1"])
    reg.decouple(u1, "2", ["C22u", "C100n"], "+3V3", refs=["C2", "C3"])
    reg.power(u1, "1", "GND")
    reg.note("C2 22u at the output keeps the AMS1117 stable.", near=u1)
    led = p.group("POWER LED")
    led.led("+3V3", "R1k", "LED_R", refs=("R3", "D1"))
    p.layout()

    m = Page(d, mc, base=0, catalog=cat)
    g = m.group("ATtiny85 MICROCONTROLLER")
    u2 = g.part("MCU", "U", ref="U2")
    g.decouple(u2, "8", ["C100n"], "+3V3", refs=["C4"])
    g.power(u2, "4", "GND")
    g.net(u2, "5", "I2C_SDA")
    g.net(u2, "7", "I2C_SCL")
    g.nc(u2, "6")
    g.series(u2, "2", "R68", "USB_D_N", ref="R7", before="PB3_USB_N")
    g.series(u2, "3", "R68", "USB_D_P", ref="R6", before="PB4_USB_P")
    g.pull(u2, "1", "R10k", "+3V3", net="RESET", ref="R4")
    q = m.group("QWIIC I2C PORT")
    j2 = q.part("JST4", "J", ref="J2")
    q.power(j2, "1", "GND")
    q.power(j2, "2", "+3V3")
    q.pull(j2, "3", "R4k7", "+3V3", net="I2C_SDA", ref="R8")
    q.pull(j2, "4", "R4k7", "+3V3", net="I2C_SCL", ref="R9")
    m.layout()
    return d.write(hw_dir)
