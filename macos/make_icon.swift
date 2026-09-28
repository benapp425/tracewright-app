// Draws Tracewright's app icon into an .iconset (macos/build.sh turns it into AppIcon.icns).
// The same design as tracewright/web/img/logo.svg: a copper trace forming a "T", with a pad at each
// end of the bar and a via at the foot of the stem, on a dark solder-mask squircle.
//
//   make_icon OUT.iconset
import Cocoa

func superellipse(_ rect: CGRect, n: CGFloat = 5) -> CGPath {
    let p = CGMutablePath()
    let a = rect.width / 2, b = rect.height / 2, cx = rect.midX, cy = rect.midY
    let steps = 720
    for i in 0...steps {
        let t = CGFloat(i) / CGFloat(steps) * 2 * .pi
        let c = cos(t), s = sin(t)
        let x = cx + a * (c < 0 ? -1 : 1) * pow(abs(c), 2 / n)
        let y = cy + b * (s < 0 ? -1 : 1) * pow(abs(s), 2 / n)
        if i == 0 { p.move(to: CGPoint(x: x, y: y)) } else { p.addLine(to: CGPoint(x: x, y: y)) }
    }
    p.closeSubpath()
    return p
}

func color(_ hex: UInt32, _ a: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255, blue: CGFloat(hex & 0xff) / 255, alpha: a)
}

func draw(_ ctx: CGContext, _ size: CGFloat) {
    let s = size / 1024
    ctx.saveGState()
    ctx.scaleBy(x: s, y: s)
    ctx.translateBy(x: 0, y: 1024)          // draw in SVG coordinates (y down)
    ctx.scaleBy(x: 1, y: -1)
    let space = CGColorSpace(name: CGColorSpace.sRGB)!
    let body = superellipse(CGRect(x: 100, y: 100, width: 824, height: 824))

    // shadow under the body
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -10 * s), blur: 26 * s, color: color(0x000000, 0.35))   // base space: not scaled by the CTM
    ctx.addPath(body)
    ctx.setFillColor(color(0x151b18))
    ctx.fillPath()
    ctx.restoreGState()

    // the solder-mask body: a soft vertical gradient
    ctx.saveGState()
    ctx.addPath(body)
    ctx.clip()
    let bg = CGGradient(colorsSpace: space, colors: [color(0x2a3530), color(0x121815)] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(bg, start: CGPoint(x: 512, y: 100), end: CGPoint(x: 512, y: 924), options: [])
    // a light edge along the top
    ctx.addPath(body)
    ctx.setLineWidth(6)
    ctx.setStrokeColor(color(0xffffff, 0.07))
    ctx.strokePath()
    ctx.restoreGState()

    // the copper: bar, stem with a 45-degree jog, two pads, a via
    let copper = CGGradient(colorsSpace: space, colors: [color(0xffc590), color(0xe8864a), color(0xb95a26)] as CFArray,
                            locations: [0, 0.55, 1])!
    let cu = CGMutablePath()
    cu.move(to: CGPoint(x: 300, y: 352)); cu.addLine(to: CGPoint(x: 724, y: 352))
    let stem = CGMutablePath()
    stem.move(to: CGPoint(x: 512, y: 352)); stem.addLine(to: CGPoint(x: 512, y: 560))
    stem.addLine(to: CGPoint(x: 600, y: 648)); stem.addLine(to: CGPoint(x: 600, y: 716))
    // separate pieces (their outlines wind differently, so they are never merged into one path)
    var pieces: [CGPath] = [cu.copy(strokingWithWidth: 84, lineCap: .round, lineJoin: .round, miterLimit: 10),
                            stem.copy(strokingWithWidth: 84, lineCap: .round, lineJoin: .round, miterLimit: 10)]
    for c in [CGPoint(x: 276, y: 352), CGPoint(x: 748, y: 352)] {
        pieces.append(CGPath(ellipseIn: CGRect(x: c.x - 78, y: c.y - 78, width: 156, height: 156), transform: nil))
    }
    pieces.append(CGPath(ellipseIn: CGRect(x: 600 - 86, y: 742 - 86, width: 172, height: 172), transform: nil))
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -6 * s), blur: 14 * s, color: color(0x000000, 0.45))
    ctx.beginTransparencyLayer(auxiliaryInfo: nil)          // one shadow for the whole copper shape
    for piece in pieces {
        ctx.saveGState()
        ctx.addPath(piece)
        ctx.clip()
        ctx.drawLinearGradient(copper, start: CGPoint(x: 230, y: 270), end: CGPoint(x: 700, y: 840), options: [])
        ctx.restoreGState()
    }
    ctx.endTransparencyLayer()
    ctx.restoreGState()
    // the via's hole and the pads' drill marks
    ctx.setFillColor(color(0x121815))
    ctx.fillEllipse(in: CGRect(x: 600 - 36, y: 742 - 36, width: 72, height: 72))
    ctx.setFillColor(color(0x7a3a16, 0.55))
    for c in [CGPoint(x: 276, y: 352), CGPoint(x: 748, y: 352)] {
        ctx.fillEllipse(in: CGRect(x: c.x - 22, y: c.y - 22, width: 44, height: 44))
    }
    ctx.restoreGState()
}

func png(_ size: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8, samplesPerPixel: 4,
                               hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    let g = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = g
    g.cgContext.interpolationQuality = .high
    g.cgContext.setAllowsAntialiasing(true)
    draw(g.cgContext, CGFloat(size))
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.iconset"
try? FileManager.default.createDirectory(atPath: out, withIntermediateDirectories: true)
for base in [16, 32, 128, 256, 512] {
    try! png(base).write(to: URL(fileURLWithPath: "\(out)/icon_\(base)x\(base).png"))
    try! png(base * 2).write(to: URL(fileURLWithPath: "\(out)/icon_\(base)x\(base)@2x.png"))
}
if CommandLine.arguments.count > 2 {                    // a large PNG too (docs, the web UI)
    try! png(1024).write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
}
