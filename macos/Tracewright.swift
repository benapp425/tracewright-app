// Tracewright for macOS: a native window around the Tracewright engine (the Python server).
//
// The app starts the engine in the background with a secret key for this launch, shows the
// interface in its own window (a WKWebView, no browser), and stops the engine when you quit. It adds
// what a browser tab cannot: real menus and shortcuts, Notification Center and a Dock badge, native
// Open / Save panels, files dropped on the window or the Dock icon, links opening in your browser,
// and a window that remembers where it was.
//
// Built by macos/build.sh (swiftc, no Xcode project). Info.plist carries the engine's Python
// (TWPython) and CPU architecture (TWArch), written by install.sh.
import Cocoa
@preconcurrency import WebKit
import UserNotifications
import UniformTypeIdentifiers
import LocalAuthentication

let appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"

func dataDir() -> URL {
    if let home = ProcessInfo.processInfo.environment["TRACEWRIGHT_HOME"], !home.isEmpty {
        return URL(fileURLWithPath: home)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Tracewright")
}

let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Tracewright.log")

func randomKey() -> String {
    var bytes = [UInt8](repeating: 0, count: 32)
    _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
    return Data(bytes).base64EncodedString()
        .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}

// MARK: - The engine (the Python server)

final class Engine {
    struct Info { let pid: Int32; let port: Int; let key: String }

    private(set) var python = ""
    private(set) var arch = ""
    private(set) var port = 0
    private(set) var key = ""
    private(set) var owns = false          // we started it (and stop it on quit)
    private var process: Process?
    private var stopping = false
    var onExit: ((Int32) -> Void)?

    init() { locate() }

    /// Where the engine's Python is: the one this app was built with, else the standard place a
    /// downloaded app installs it (~/.tracewright-app), with the architecture its installer recorded.
    func locate() {
        let info = Bundle.main.infoDictionary ?? [:]
        let built = (info["TWPython"] as? String) ?? ""
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        if !built.isEmpty && FileManager.default.isExecutableFile(atPath: built) {
            python = built
            arch = (info["TWArch"] as? String) ?? ""
        } else {
            python = "\(home)/.tracewright-app/venv/bin/python"
            arch = ((try? String(contentsOfFile: "\(home)/.tracewright-app/arch", encoding: .utf8)) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    var installed: Bool { FileManager.default.isExecutableFile(atPath: python) }

    /// The installer and source a downloaded app carries (Contents/Resources/engine).
    var bundledInstaller: String? {
        guard let r = Bundle.main.resourcePath else { return nil }
        let p = r + "/engine/install.sh"
        return FileManager.default.fileExists(atPath: p) ? p : nil
    }

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    /// Run the `tracewright` command (python -m tracewright ARGS) and wait for it: (exit status, output).
    /// `input` goes to its standard input (a password never goes on the command line).
    func command(_ args: [String], input: String? = nil) -> (Int32, String) {
        let p = Process()
        if !arch.isEmpty && FileManager.default.isExecutableFile(atPath: "/usr/bin/arch") {
            p.executableURL = URL(fileURLWithPath: "/usr/bin/arch")
            p.arguments = ["-" + arch, python, "-m", "tracewright"] + args
        } else {
            p.executableURL = URL(fileURLWithPath: python)
            p.arguments = ["-m", "tracewright"] + args
        }
        let out = Pipe(), inp = Pipe()
        p.standardOutput = out
        p.standardError = out
        p.standardInput = inp
        p.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        do { try p.run() } catch { return (-1, error.localizedDescription) }
        if let s = input, let d = (s + "\n").data(using: .utf8) { inp.fileHandleForWriting.write(d) }
        try? inp.fileHandleForWriting.close()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// The running server's record (server.json), when its process is alive.
    func readServerFile() -> Info? {
        let url = dataDir().appendingPathComponent("server.json")
        guard let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let pid = (obj["pid"] as? NSNumber)?.int32Value, let port = (obj["port"] as? NSNumber)?.intValue,
              let key = obj["key"] as? String, !key.isEmpty, kill(pid, 0) == 0 else { return nil }
        return Info(pid: pid, port: port, key: key)
    }

    func healthy(_ port: Int, timeout: TimeInterval = 1.5) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/health") else { return false }
        var req = URLRequest(url: url)
        req.timeoutInterval = timeout
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            ok = (resp as? HTTPURLResponse)?.statusCode == 200 && (data.map { String(decoding: $0, as: UTF8.self).contains("\"ok\"") } ?? false)
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 0.5)
        return ok
    }

    /// Attach to a running Tracewright, or start one; calls back on the main queue.
    func start(_ done: @escaping (String?) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let err = self.startSync()
            DispatchQueue.main.async { done(err) }
        }
    }

    private func startSync() -> String? {
        if let info = readServerFile(), healthy(info.port) {
            port = info.port; key = info.key; owns = false
            return nil
        }
        guard !python.isEmpty, FileManager.default.isExecutableFile(atPath: python) else {
            return "Tracewright's Python was not found (\(python.isEmpty ? "not set" : python)). Run ./install.sh again."
        }
        key = randomKey()
        let p = Process()
        if !arch.isEmpty && FileManager.default.isExecutableFile(atPath: "/usr/bin/arch") {
            p.executableURL = URL(fileURLWithPath: "/usr/bin/arch")
            p.arguments = ["-" + arch, python, "-m", "tracewright", "serve", "--app"]
        } else {
            p.executableURL = URL(fileURLWithPath: python)
            p.arguments = ["-m", "tracewright", "serve", "--app"]
        }
        var env = ProcessInfo.processInfo.environment
        env["TW_LOCAL_KEY"] = key
        env["TW_PARENT_PID"] = String(getpid())
        env["PYTHONUNBUFFERED"] = "1"
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let extra = ["/opt/homebrew/bin", "/usr/local/bin", "\(home)/.local/bin", "\(home)/.claude/local"]
        env["PATH"] = (extra + [(env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")]).joined(separator: ":")
        p.environment = env
        p.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        try? FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let h = try? FileHandle(forWritingTo: logURL) {
            h.seekToEndOfFile()
            h.write("\n--- Tracewright app \(appVersion) starting the engine \(Date())\n".data(using: .utf8)!)
            p.standardOutput = h
            p.standardError = h
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self = self, !self.stopping else { return }
                self.process = nil
                self.onExit?(proc.terminationStatus)
            }
        }
        do { try p.run() } catch { return "The engine could not be started: \(error.localizedDescription)" }
        process = p
        owns = true
        stopping = false
        let deadline = Date().addingTimeInterval(90)
        while Date() < deadline {
            if !p.isRunning { return "The engine stopped while starting (exit \(p.terminationStatus))." }
            if let info = readServerFile(), info.pid == p.processIdentifier, healthy(info.port, timeout: 1) {
                port = info.port
                return nil
            }
            Thread.sleep(forTimeInterval: 0.25)
        }
        return "The engine did not answer within 90 seconds."
    }

    /// Projects where Claude (or a long job) is working now.
    func activity(_ done: @escaping ([String]) -> Void) {
        guard port > 0, let url = URL(string: "http://127.0.0.1:\(port)/api/activity") else { done([]); return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2
        req.setValue(key, forHTTPHeaderField: "X-Tracewright-Key")
        URLSession.shared.dataTask(with: req) { data, _, _ in
            var names: [String] = []
            if let data = data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let busy = obj["busy"] as? [[String: Any]] {
                names = busy.compactMap { $0["name"] as? String }
            }
            DispatchQueue.main.async { done(names) }
        }.resume()
    }

    func stop(_ done: @escaping () -> Void) {
        guard let p = process, owns, p.isRunning else { done(); return }
        stopping = true
        p.terminate()
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(6)
            while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
            DispatchQueue.main.async { done() }
        }
    }
}

// MARK: - The web view: dragging the window by its header, files dropped on it

final class AppWebView: WKWebView {
    var dragRegions: [CGRect] = []         // CSS px, from the top left: the header's empty space
    var dragHoles: [CGRect] = []           // controls inside those regions
    var onFiles: ((String, [URL]) -> Void)?
    private var fileDrag = false

    private func cssPoint(_ event: NSEvent) -> CGPoint {
        let p = convert(event.locationInWindow, from: nil)
        let y = isFlipped ? p.y : bounds.height - p.y
        let z = max(pageZoom, 0.1)
        return CGPoint(x: p.x / z, y: y / z)
    }

    override func mouseDown(with event: NSEvent) {
        let p = cssPoint(event)
        if dragRegions.contains(where: { $0.contains(p) }) && !dragHoles.contains(where: { $0.contains(p) }) {
            if event.clickCount == 2 {
                switch UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") ?? "Maximize" {
                case "Minimize": window?.miniaturize(nil)
                case "None": break
                default: window?.zoom(nil)
                }
                return
            }
            window?.performDrag(with: event)
            return
        }
        super.mouseDown(with: event)
    }

    private func files(_ info: NSDraggingInfo) -> [URL]? {
        let urls = info.draggingPasteboard.readObjects(forClasses: [NSURL.self],
                                                        options: [.urlReadingFileURLsOnly: true]) as? [URL]
        return (urls?.isEmpty ?? true) ? nil : urls
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        if files(sender) != nil { fileDrag = true; onFiles?("enter", []); return .copy }
        fileDrag = false
        return super.draggingEntered(sender)
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        fileDrag ? .copy : super.draggingUpdated(sender)
    }

    override func draggingExited(_ sender: NSDraggingInfo?) {
        if fileDrag { fileDrag = false; onFiles?("exit", []); return }
        super.draggingExited(sender)
    }

    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool {
        fileDrag ? true : super.prepareForDragOperation(sender)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        if fileDrag, let urls = files(sender) { fileDrag = false; onFiles?("drop", urls); return true }
        return super.performDragOperation(sender)
    }

    override func concludeDragOperation(_ sender: NSDraggingInfo?) {
        if !fileDrag { super.concludeDragOperation(sender) }
    }
}

// MARK: - The app

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
                         WKScriptMessageHandlerWithReply, WKDownloadDelegate, UNUserNotificationCenterDelegate,
                         NSMenuItemValidation {
    var window: NSWindow!
    var web: AppWebView!
    let engine = Engine()
    var inProject = false
    var loaded = false
    var quitting = false
    var pendingOpen: [URL] = []
    var titleObs: NSKeyValueObservation?
    var downloads: [ObjectIdentifier: URL] = [:]
    var extraWindows: [NSWindow] = []
    var selfTesting = false
    var container: NSView!
    var bootStart = Date()
    // the app lock: Touch ID (or the Mac's password) to open Tracewright, after idling and when the Mac sleeps
    var locked = false
    var lockView: LockView?
    var lastActivity = Date()
    var idleTimer: Timer?

    // ------------------------------------------------------------------ a development self-test
    // TW_SELFTEST=DIR: once the page is up, photograph the window, go to TW_SELFTEST_ROUTE and photograph
    // it again, write what the page reports (native mode, drag regions, script errors) to report.json, quit.
    func selfTest(_ dir: String) {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        func shot(_ name: String, _ next: @escaping () -> Void) {
            web.takeSnapshot(with: nil) { image, _ in
                if let img = image, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
                   let png = rep.representation(using: .png, properties: [:]) {
                    try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name))
                }
                next()
            }
        }
        func report(_ name: String, _ next: @escaping () -> Void) {
            let js = "JSON.stringify({route: location.hash, title: document.title, native: document.documentElement.classList.contains('native'), " +
                     "theme: document.documentElement.dataset.theme, errors: window.__errors, drags: document.querySelectorAll('[data-drag]').length})"
            web.evaluateJavaScript(js) { value, error in
                var out: [String: Any] = ["page": value ?? "", "error": error.map { "\($0)" } ?? "",
                                          "dragRegions": self.web.dragRegions.map { [$0.minX, $0.minY, $0.width, $0.height] },
                                          "dragHoles": self.web.dragHoles.count, "zoom": self.web.pageZoom,
                                          "window": [self.window.frame.width, self.window.frame.height]]
                // a click in the header's empty space drags; one on a control goes to the page
                if let r = self.web.dragRegions.first {
                    let p = CGPoint(x: r.midX, y: r.midY)
                    out["headerMiddleDrags"] = !self.web.dragHoles.contains(where: { $0.contains(p) })
                }
                if let d = try? JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted]) {
                    try? d.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name))
                }
                next()
            }
        }
        let route = ProcessInfo.processInfo.environment["TW_SELFTEST_ROUTE"] ?? ""
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
            shot("1-home.png") { report("1-home.json") {
                self.web.evaluateJavaScript("location.hash = \(String(reflecting: route))") { _, _ in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 6) {
                        shot("2-route.png") { report("2-route.json") {
                            self.web.evaluateJavaScript("window.twApp.command('palette')") { _, _ in
                                DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                                    shot("3-palette.png") { NSApp.terminate(nil) }
                                }
                            }
                        } }
                    }
                }
            } }
        }
    }

    // ------------------------------------------------------------------ launch
    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.mainMenu = buildMenu()
        makeWindow()
        UNUserNotificationCenter.current().delegate = self
        setupLock()
        startEngine()
    }

    func makeWindow() {
        let config = WKWebViewConfiguration()
        let ucc = WKUserContentController()
        ucc.addScriptMessageHandler(self, contentWorld: .page, name: "tw")
        let boot = """
        document.documentElement.classList.add('native');
        window.twNativeInfo = {version: "\(appVersion)", fullscreen: false};
        window.__errors = [];
        addEventListener('error', (e) => window.__errors.push(String(e.message || e)));
        addEventListener('unhandledrejection', (e) => window.__errors.push(String(e.reason && e.reason.message || e.reason)));
        """
        ucc.addUserScript(WKUserScript(source: boot, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        config.userContentController = ucc
        config.applicationNameForUserAgent = "Tracewright/\(appVersion)"
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        web = AppWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        web.allowsMagnification = false
        web.setValue(false, forKey: "drawsBackground")
        if #available(macOS 13.3, *) { web.isInspectable = true }
        let z = UserDefaults.standard.double(forKey: "pageZoom")
        web.pageZoom = z > 0 ? z : 1.0
        web.onFiles = { [weak self] phase, urls in self?.filesDragged(phase, urls) }

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1380, height: 880),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Tracewright"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.toolbar = NSToolbar(identifier: "main")       // a 52 pt title bar: the traffic lights centred in the header
        window.toolbarStyle = .unified
        window.titlebarSeparatorStyle = .none
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.minSize = NSSize(width: 980, height: 620)
        window.delegate = self
        window.backgroundColor = NSColor(srgbRed: 0.067, green: 0.071, blue: 0.078, alpha: 1)
        container = NSView(frame: NSRect(x: 0, y: 0, width: 1280, height: 820))
        web.frame = container.bounds
        web.autoresizingMask = [.width, .height]
        container.addSubview(web)
        window.contentView = container
        if !window.setFrameUsingName("TracewrightMain") { window.center() }
        window.setFrameAutosaveName("TracewrightMain")
        titleObs = web.observe(\.title, options: [.new]) { [weak self] wv, _ in
            if let t = wv.title, !t.isEmpty { self?.window.title = t }
        }
        showBoot()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func startEngine() {
        if (!engine.installed && engine.readServerFile() == nil && engine.bundledInstaller != nil)
            || ProcessInfo.processInfo.environment["TW_FORCE_INSTALLER"] == "1" {
            showInstaller()                                 // a downloaded app, first launch: set up the engine
            return
        }
        engine.onExit = { [weak self] status in self?.engineStopped(status) }
        engine.start { [weak self] err in
            guard let self = self else { return }
            if let err = err { self.showError(err); return }
            // the boot animation plays to the end before the app replaces it
            let wait = max(0, 1.9 - Date().timeIntervalSince(self.bootStart))
            DispatchQueue.main.asyncAfter(deadline: .now() + wait) { self.loadApp() }
        }
    }

    func loadApp(route: String = "") {
        let url = route.isEmpty ? engine.baseURL : (URL(string: route, relativeTo: engine.baseURL) ?? engine.baseURL)
        var req = URLRequest(url: url)
        req.setValue(engine.key, forHTTPHeaderField: "X-Tracewright-Key")
        loaded = false
        web.load(req)
    }

    func engineStopped(_ status: Int32) {
        if quitting { return }
        showError("The Tracewright engine stopped (exit \(status)).", restart: true)
    }

    // ------------------------------------------------------------------ native pages (starting, errors)
    func showPage(title: String, body: String, spinner: Bool = false, buttons: [(String, String)] = [], log: String = "") {
        func esc(_ s: String) -> String {
            s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
        }
        let btns = buttons.map { "<button onclick=\"webkit.messageHandlers.tw.postMessage({type:'page',action:'\($0.1)'})\">\(esc($0.0))</button>" }.joined()
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><style>
        :root{color-scheme:dark light}
        body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#111214;color:#ececee;
          font:13px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;-webkit-user-select:none;cursor:default}
        .box{text-align:center;max-width:560px;padding:24px}
        h1{font-size:17px;font-weight:600;margin:18px 0 6px} p{color:#9a9ca4;margin:0 0 16px}
        .sp{width:18px;height:18px;border:2px solid #34353b;border-top-color:#e8864a;border-radius:50%;animation:s .8s linear infinite;margin:0 auto}
        @keyframes s{to{transform:rotate(360deg)}}
        button{font:inherit;font-weight:500;color:#ececee;background:#26272c;border:1px solid #34353b;border-radius:7px;padding:6px 14px;margin:0 4px;cursor:default}
        button:first-of-type{background:#e8864a;border-color:#e8864a;color:#1b1009}
        pre{text-align:left;font:11px ui-monospace,Menlo,monospace;color:#9a9ca4;background:#17181b;border:1px solid #2b2c31;border-radius:8px;
          padding:10px;max-height:220px;overflow:auto;white-space:pre-wrap;-webkit-user-select:text;margin:18px 0 0}
        @media (prefers-color-scheme: light){body{background:#f5f5f7;color:#17181c}p{color:#5f616a}button{background:#fff;border-color:#d8d8dd;color:#17181c}
          pre{background:#fff;border-color:#e2e2e6;color:#5f616a}}
        </style></head><body><div class="box">\(Self.logoSVG)<h1>\(esc(title))</h1><p>\(esc(body))</p>
        \(spinner ? "<div class=\"sp\"></div>" : "")<div>\(btns)</div>\(log.isEmpty ? "" : "<pre>\(esc(log))</pre>")</div></body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    /// The boot screen while the engine starts: the logo draws itself (its pads land, the traces run
    /// between them), then the name.
    func showBoot() {
        bootStart = Date()
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><style>
        :root{color-scheme:dark light}
        body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#111214;color:#ececee;
          font:13px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;-webkit-user-select:none;cursor:default;overflow:hidden}
        body::before{content:"";position:fixed;inset:0;background:radial-gradient(520px 320px at 50% 44%,rgba(232,134,74,.13),transparent 70%);animation:bg 1.6s ease-out both}
        @keyframes bg{from{opacity:0}}
        .box{position:relative;text-align:center}
        .logo{display:block;margin:0 auto;animation:glow 1.3s 1.15s ease-out both}
        @keyframes glow{35%{filter:drop-shadow(0 0 26px rgba(232,134,74,.6))}100%{filter:drop-shadow(0 0 0 rgba(232,134,74,0))}}
        .tile{transform-box:fill-box;transform-origin:center;animation:tile .55s cubic-bezier(.2,.8,.2,1) both}
        @keyframes tile{from{opacity:0;transform:scale(.82)}}
        .pad{transform-box:fill-box;transform-origin:center;animation:pad .38s cubic-bezier(.3,1.7,.5,1) both}
        .p1{animation-delay:.28s}.p2{animation-delay:.36s}.p3,.hole{animation-delay:1.02s}
        @keyframes pad{from{transform:scale(0)}}
        .tr{stroke-dasharray:1;stroke-dashoffset:1;animation:draw .42s cubic-bezier(.5,0,.3,1) forwards}
        .t1{animation-delay:.42s}.t2{animation-delay:.66s}
        @keyframes draw{to{stroke-dashoffset:0}}
        .word{font-size:25px;font-weight:650;letter-spacing:-.015em;margin-top:22px;animation:rise .55s 1.1s cubic-bezier(.2,.8,.2,1) both}
        .tag{color:#9a9ca4;margin-top:2px;animation:rise .55s 1.24s cubic-bezier(.2,.8,.2,1) both}
        .st{margin-top:30px;font-size:12px;color:#6d6f78;animation:rise .4s 1.45s both;display:flex;align-items:center;justify-content:center;gap:8px}
        .st i{width:6px;height:6px;border-radius:50%;background:#e8864a;animation:b 1.2s ease-in-out infinite}
        @keyframes b{50%{opacity:.3}}
        @keyframes rise{from{opacity:0;transform:translateY(8px)}}
        @media (prefers-reduced-motion: reduce){*{animation-duration:.01s!important;animation-delay:0s!important}}
        @media (prefers-color-scheme: light){body{background:#f5f5f7;color:#17181c}.tag{color:#5f616a}.st{color:#8a8c94}}
        </style></head><body><div class="box">
        <svg class="logo" width="104" height="104" viewBox="100 100 824 824"><defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#2a3530"/><stop offset="1" stop-color="#121815"/></linearGradient>
        <linearGradient id="cu" x1="230" y1="270" x2="700" y2="840" gradientUnits="userSpaceOnUse">
        <stop offset="0" stop-color="#ffc590"/><stop offset=".55" stop-color="#e8864a"/><stop offset="1" stop-color="#b95a26"/></linearGradient></defs>
        <rect class="tile" x="100" y="100" width="824" height="824" rx="190" fill="url(#bg)"/>
        <g fill="none" stroke="url(#cu)" stroke-linecap="round" stroke-linejoin="round" stroke-width="84">
        <path class="tr t1" pathLength="1" d="M300 352H724"/><path class="tr t2" pathLength="1" d="M512 352V560L600 648V716"/></g>
        <g fill="url(#cu)"><circle class="pad p1" cx="276" cy="352" r="78"/><circle class="pad p2" cx="748" cy="352" r="78"/><circle class="pad p3" cx="600" cy="742" r="86"/></g>
        <circle class="pad hole" cx="600" cy="742" r="36" fill="#121815"/>
        <g fill="#7a3a16" fill-opacity=".55"><circle class="pad p1" cx="276" cy="352" r="22"/><circle class="pad p2" cx="748" cy="352" r="22"/></g></svg>
        <div class="word">Tracewright</div><div class="tag">KiCad boards, designed with Claude</div>
        <div class="st"><i></i>Starting the engine</div></div></body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    /// First launch of a downloaded app: what this Mac has, and one button to install the engine.
    func showInstaller() {
        guard let installer = engine.bundledInstaller else { return }
        var check: [String: Any] = [:]
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = [installer, "--check"]
        let pipe = Pipe()
        p.standardOutput = pipe
        try? p.run()
        p.waitUntilExit()
        if let obj = try? JSONSerialization.jsonObject(with: pipe.fileHandleForReading.readDataToEndOfFile()) as? [String: Any] { check = obj }
        let py = (check["python"] as? String) ?? "", ver = (check["version"] as? String) ?? "", kicad = (check["kicad"] as? String) ?? ""
        func row(_ ok: Bool, _ title: String, _ text: String, _ action: String?, _ label: String) -> String {
            let btn = action.map { "<button class=\"small\" onclick=\"webkit.messageHandlers.tw.postMessage({type:'page',action:'\($0)'})\">\(label)</button>" } ?? ""
            return "<div class=\"row \(ok ? "ok" : "no")\"><span class=\"dot\">\(ok ? "✓" : "!")</span><div><b>\(title)</b><span>\(text)</span></div>\(ok ? "" : btn)</div>"
        }
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><style>
        :root{color-scheme:dark light}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#111214;color:#ececee;
          font:13.5px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;-webkit-user-select:none;cursor:default}
        .box{width:520px;padding:30px}h1{font-size:22px;margin:18px 0 6px}p{color:#9a9ca4;margin:0 0 18px}
        .row{display:flex;align-items:center;gap:12px;padding:11px 13px;border:1px solid #2b2c31;border-radius:11px;margin-bottom:8px;background:#17181b}
        .row b{display:block}.row span:not(.dot){display:block;color:#9a9ca4;font-size:12.5px}.row>div{flex:1}
        .dot{width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;flex:none}
        .ok .dot{background:rgba(67,194,131,.15);color:#43c283}.no .dot{background:rgba(232,184,74,.15);color:#e8b84a}
        button{font:inherit;font-weight:600;color:#1b1009;background:#e8864a;border:0;border-radius:9px;padding:9px 18px;cursor:default}
        button.small{padding:5px 11px;font-size:12px;background:#26272c;color:#ececee;border:1px solid #34353b}
        button:disabled{opacity:.4}.go{margin-top:16px;display:flex;gap:10px;align-items:center}
        pre{font:11px ui-monospace,Menlo,monospace;color:#9a9ca4;background:#0b0c0e;border:1px solid #2b2c31;border-radius:9px;padding:10px;height:150px;overflow:auto;
          white-space:pre-wrap;display:none;margin-top:14px;-webkit-user-select:text}
        @media (prefers-color-scheme: light){body{background:#f5f5f7;color:#17181c}p{color:#5f616a}.row{background:#fff;border-color:#e2e2e6}pre{background:#fff;border-color:#e2e2e6}}
        </style></head><body><div class="box">\(Self.logoSVG)
        <h1>Set up Tracewright</h1><p>Tracewright installs its engine in ~/.tracewright-app (about 150 MB). Nothing else on your Mac is changed.</p>
        \(row(!py.isEmpty, "Python 3.10 or newer", py.isEmpty ? "Not found. Install it from python.org or Homebrew." : "Python \(ver) at \(py)", "python", "Get Python"))
        \(row(!kicad.isEmpty, "KiCad 9 or 10", kicad.isEmpty ? "Not found. You can install it later." : "Found at \(kicad)", "kicad", "Get KiCad"))
        <div class="go"><button id="go" \(py.isEmpty ? "disabled" : "") onclick="this.disabled=true;this.textContent='Installing…';document.getElementById('log').style.display='block';webkit.messageHandlers.tw.postMessage({type:'page',action:'install'})">Install the engine</button>
        <button class="small" onclick="webkit.messageHandlers.tw.postMessage({type:'page',action:'recheck'})">Check again</button></div>
        <pre id="log"></pre></div><script>function addLog(t){const l=document.getElementById('log');l.style.display='block';l.textContent+=t;l.scrollTop=l.scrollHeight;}</script></body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    func runInstaller() {
        guard let installer = engine.bundledInstaller else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = [installer, "--engine-only"]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty, let t = String(data: d, encoding: .utf8),
                  let js = try? JSONSerialization.data(withJSONObject: [t]), let arg = String(data: js, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.web.evaluateJavaScript("addLog(\(arg)[0])", completionHandler: nil) }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                pipe.fileHandleForReading.readabilityHandler = nil
                guard let self = self else { return }
                if proc.terminationStatus == 0 {
                    self.engine.locate()
                    self.showBoot()
                    self.startEngine()
                } else {
                    self.web.evaluateJavaScript("addLog('\\nThe install stopped (exit \(proc.terminationStatus)). Fix what it says above, then Check again.\\n')", completionHandler: nil)
                }
            }
        }
        do { try p.run() } catch { showError("The installer could not start: \(error.localizedDescription)") }
    }

    func showError(_ message: String, restart: Bool = false) {
        showPage(title: restart ? "Tracewright stopped" : "Tracewright could not start", body: message,
                 buttons: [(restart ? "Restart" : "Try Again", "retry"), ("Show Log", "log")], log: tailOfLog())
    }

    func tailOfLog(_ lines: Int = 14) -> String {
        guard let txt = try? String(contentsOf: logURL, encoding: .utf8) else { return "" }
        // the engine's own lines since this app started it, without the SDK's warnings
        let all = txt.split(separator: "\n", omittingEmptySubsequences: false)
        let start = all.lastIndex(where: { $0.hasPrefix("--- Tracewright app") }) ?? all.startIndex
        return all[start...].filter { !$0.contains("Warning") && !$0.hasPrefix("  _warn") }.suffix(lines).joined(separator: "\n")
    }

    // the same drawing as tracewright/web/img/logo.svg (the copper gradient in user space: a horizontal
    // line has no height, so a gradient sized to its bounding box would paint nothing)
    static let logoSVG = """
    <svg width="72" height="72" viewBox="100 100 824 824"><defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#2a3530"/><stop offset="1" stop-color="#121815"/></linearGradient>
    <linearGradient id="cu" x1="230" y1="270" x2="700" y2="840" gradientUnits="userSpaceOnUse">
    <stop offset="0" stop-color="#ffc590"/><stop offset=".55" stop-color="#e8864a"/><stop offset="1" stop-color="#b95a26"/></linearGradient></defs>
    <rect x="100" y="100" width="824" height="824" rx="190" fill="url(#bg)"/>
    <g fill="none" stroke="url(#cu)" stroke-linecap="round" stroke-linejoin="round" stroke-width="84"><path d="M300 352H724"/><path d="M512 352V560L600 648V716"/></g>
    <g fill="url(#cu)"><circle cx="276" cy="352" r="78"/><circle cx="748" cy="352" r="78"/><circle cx="600" cy="742" r="86"/></g>
    <circle cx="600" cy="742" r="36" fill="#121815"/><g fill="#7a3a16" fill-opacity=".55"><circle cx="276" cy="352" r="22"/><circle cx="748" cy="352" r="22"/></g></svg>
    """

    // ------------------------------------------------------------------ the bridge
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard let m = message.body as? [String: Any], let type = m["type"] as? String else { replyHandler(nil, nil); return }
        switch type {
        case "ready":
            loaded = true
            if !pendingOpen.isEmpty { send(["type": "open", "paths": pendingOpen.map { $0.path }]); pendingOpen = [] }
            replyHandler(["version": appVersion], nil)
            if let dir = ProcessInfo.processInfo.environment["TW_SELFTEST"], !selfTesting { selfTesting = true; selfTest(dir) }
        case "context":
            inProject = (m["project"] as? String).map { !$0.isEmpty } ?? false
            replyHandler(nil, nil)
        case "drag":
            web.dragRegions = rects(m["regions"])
            web.dragHoles = rects(m["holes"])
            replyHandler(nil, nil)
        case "notify":
            notify(title: m["title"] as? String ?? "Tracewright", body: m["body"] as? String ?? "", pid: m["pid"] as? String,
                   always: m["always"] as? Bool ?? false)
            replyHandler(nil, nil)
        case "badge":
            let t = m["text"] as? String ?? ""
            NSApp.dockTile.badgeLabel = t.isEmpty ? nil : t
            replyHandler(nil, nil)
        case "attention":
            if !NSApp.isActive { NSApp.requestUserAttention(.informationalRequest) }
            replyHandler(nil, nil)
        case "activate":
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
            replyHandler(nil, nil)
        case "open":
            if let s = m["url"] as? String, let u = URL(string: s), ["http", "https", "mailto"].contains(u.scheme ?? "") {
                NSWorkspace.shared.open(u)
            }
            replyHandler(nil, nil)
        case "openPath":
            if let p = m["path"] as? String, FileManager.default.fileExists(atPath: p) {
                NSWorkspace.shared.open(URL(fileURLWithPath: p))
            }
            replyHandler(nil, nil)
        case "reveal":
            if let p = m["path"] as? String { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)]) }
            replyHandler(nil, nil)
        case "pick":
            pick(m) { replyHandler($0, nil) }
        case "save":
            if let s = m["url"] as? String, let u = URL(string: s, relativeTo: engine.baseURL) {
                web.startDownload(using: URLRequest(url: u)) { d in d.delegate = self }
            }
            replyHandler(nil, nil)
        case "theme":
            applyTheme(m["mode"] as? String ?? "system", bg: m["bg"] as? String)
            replyHandler(nil, nil)
        case "lockState":
            let ctx = LAContext()
            var e: NSError?
            replyHandler(["enabled": lockEnabled, "minutes": lockMinutes, "available": ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &e),
                          "biometrics": LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil), "locked": locked], nil)
        case "lockSet":
            let on = m["enabled"] as? Bool ?? lockEnabled
            if let mins = m["minutes"] as? Int { UserDefaults.standard.set(mins, forKey: "lockAfterMinutes") }
            if on && !lockEnabled {
                // turning it on: prove the unlock works first, so nobody locks themselves out
                authenticate("turn on the Tracewright lock") { ok in
                    if ok { UserDefaults.standard.set(true, forKey: "lockEnabled"); self.lastActivity = Date() }
                    replyHandler(["enabled": self.lockEnabled, "minutes": self.lockMinutes], nil)
                }
            } else {
                if !on { UserDefaults.standard.set(false, forKey: "lockEnabled") }
                replyHandler(["enabled": lockEnabled, "minutes": lockMinutes], nil)
            }
        case "lockNow":
            replyHandler(nil, nil)
            if lockEnabled { lock() }
        case "resetPassword":
            // A forgotten password: whoever proves they are this Mac's user (Touch ID or the Mac's
            // password) sets a new one, as `tracewright account reset EMAIL` does in Terminal.
            let email = (m["email"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let pw = m["password"] as? String ?? ""
            guard !email.isEmpty, !email.hasPrefix("-"), !pw.isEmpty else { replyHandler(["ok": false, "message": "Enter the email and a new password."], nil); return }
            guard engine.installed else { replyHandler(["ok": false, "message": "Tracewright's engine is not installed."], nil); return }
            authenticate("reset the Tracewright password for \(email)") { ok in
                guard ok else { replyHandler(["ok": false, "cancelled": true], nil); return }
                DispatchQueue.global(qos: .userInitiated).async {
                    let (status, out) = self.engine.command(["account", "reset", email, "--stdin"], input: pw)
                    DispatchQueue.main.async { replyHandler(["ok": status == 0, "message": out], nil) }
                }
            }
        case "quit":
            replyHandler(nil, nil)
            NSApp.terminate(nil)
        case "page":
            replyHandler(nil, nil)
            switch m["action"] as? String {
            case "retry":
                showBoot()
                startEngine()
            case "log": NSWorkspace.shared.open(logURL)
            case "install": runInstaller()
            case "recheck": showInstaller()
            case "python": NSWorkspace.shared.open(URL(string: "https://www.python.org/downloads/macos/")!)
            case "kicad": NSWorkspace.shared.open(URL(string: "https://www.kicad.org/download/macos/")!)
            default: break
            }
        default:
            replyHandler(nil, "unknown message \(type)")
        }
    }

    func rects(_ any: Any?) -> [CGRect] {
        (any as? [[Double]] ?? []).compactMap { $0.count == 4 ? CGRect(x: $0[0], y: $0[1], width: $0[2], height: $0[3]) : nil }
    }

    /// A message to the page: window.twNative.receive({...}).
    func send(_ msg: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: msg), let js = String(data: data, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.twNative && window.twNative.receive(\(js))", completionHandler: nil)
    }

    func command(_ name: String, _ arg: Any? = nil) {
        var msg: [String: Any] = ["type": "command", "name": name]
        if let a = arg { msg["arg"] = a }
        send(msg)
    }

    func applyTheme(_ mode: String, bg: String?) {
        switch mode {
        case "dark": NSApp.appearance = NSAppearance(named: .darkAqua)
        case "light": NSApp.appearance = NSAppearance(named: .aqua)
        default: NSApp.appearance = nil
        }
        if let bg = bg, let c = NSColor(hex: bg) {
            window.backgroundColor = c
            if #available(macOS 12.0, *) { web.underPageBackgroundColor = c }
        }
    }

    func pick(_ m: [String: Any], _ done: @escaping (Any?) -> Void) {
        let kind = m["kind"] as? String ?? "file"
        let panel = NSOpenPanel()
        panel.canChooseDirectories = kind == "folder" || kind == "any"
        panel.canChooseFiles = kind != "folder"
        panel.allowsMultipleSelection = m["multiple"] as? Bool ?? false
        panel.message = m["title"] as? String ?? ""
        panel.prompt = m["prompt"] as? String ?? "Choose"
        if let exts = m["types"] as? [String], !exts.isEmpty, kind != "folder" {
            panel.allowedContentTypes = exts.compactMap { UTType(filenameExtension: $0) }
        }
        panel.beginSheetModal(for: window) { resp in
            guard resp == .OK else { done(nil); return }
            let paths = panel.urls.map { $0.path }
            done(panel.allowsMultipleSelection ? paths : paths.first)
        }
    }

    func filesDragged(_ phase: String, _ urls: [URL]) {
        send(["type": "files", "phase": phase, "paths": urls.map { $0.path }])
    }

    // ------------------------------------------------------------------ notifications
    func notify(title: String, body: String, pid: String?, always: Bool) {
        if NSApp.isActive && !always { return }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            guard granted else { return }
            let c = UNMutableNotificationContent()
            c.title = title
            c.body = body
            c.sound = .default
            if let pid = pid { c.userInfo = ["pid": pid] }
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let pid = response.notification.request.content.userInfo["pid"] as? String
        DispatchQueue.main.async {
            self.window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            if let pid = pid { self.command("open-project", pid) }
        }
        done()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }

    // ------------------------------------------------------------------ navigation, windows, downloads
    func isOurs(_ url: URL?) -> Bool {
        guard let u = url else { return false }
        if u.scheme == "about" || u.scheme == "blob" || u.scheme == "data" { return true }
        return u.scheme == "http" && (u.host == "127.0.0.1" || u.host == "localhost") && (u.port ?? 80) == engine.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.shouldPerformDownload { decisionHandler(.download); return }
        if let u = action.request.url, !isOurs(u) {
            if ["http", "https", "mailto"].contains(u.scheme ?? "") { NSWorkspace.shared.open(u) }
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disp = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        if disp.lowercased().hasPrefix("attachment") || !response.canShowMIMEType { decisionHandler(.download); return }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if isOurs(webView.url) && webView.url?.scheme == "http" { loaded = true }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code == NSURLErrorCancelled { return }
        if isOurs(webView.url) || webView.url == nil { showError("The window could not reach the engine: \(error.localizedDescription)", restart: true) }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if engine.port > 0 { loadApp() }                  // the page's process crashed: load it again
    }

    // target=_blank: our own pages (a PDF, an image) in a window of their own, anything else in the browser
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for action: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard let u = action.request.url else { return nil }
        if !isOurs(u) {
            if ["http", "https", "mailto"].contains(u.scheme ?? "") { NSWorkspace.shared.open(u) }
            return nil
        }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 1000), styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        let v = WKWebView(frame: .zero, configuration: configuration)
        v.navigationDelegate = self
        w.contentView = v
        w.title = u.lastPathComponent
        w.isReleasedWhenClosed = false
        w.center()
        w.makeKeyAndOrderFront(nil)
        extraWindows.append(w)
        return v
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert()
        a.messageText = prompt
        let f = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        f.stringValue = defaultText ?? ""
        a.accessoryView = f
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn ? f.stringValue : nil) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.beginSheetModal(for: window) { r in
            guard r == .OK, let url = panel.url else { completionHandler(nil); return }
            try? FileManager.default.removeItem(at: url)             // the panel asked before replacing it
            self.downloads[ObjectIdentifier(download)] = url
            completionHandler(url)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let url = downloads.removeValue(forKey: ObjectIdentifier(download)) {
            send(["type": "downloaded", "path": url.path, "name": url.lastPathComponent])
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        downloads.removeValue(forKey: ObjectIdentifier(download))
        send(["type": "toast", "level": "error", "text": "Download failed: \(error.localizedDescription)"])
    }

    // ------------------------------------------------------------------ app events
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window.makeKeyAndOrderFront(nil) }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationDidBecomeActive(_ note: Notification) {
        NSApp.dockTile.badgeLabel = nil
        send(["type": "focus", "active": true])
    }

    func applicationDidResignActive(_ note: Notification) { send(["type": "focus", "active": false]) }

    func application(_ application: NSApplication, open urls: [URL]) {
        window.makeKeyAndOrderFront(nil)
        if loaded { send(["type": "open", "paths": urls.map { $0.path }]) } else { pendingOpen += urls }
    }

    func windowDidEnterFullScreen(_ note: Notification) { send(["type": "fullscreen", "on": true]) }
    func windowDidExitFullScreen(_ note: Notification) { send(["type": "fullscreen", "on": false]) }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting || !engine.owns { quitting = true; return .terminateNow }
        engine.activity { busy in
            if !busy.isEmpty {
                let a = NSAlert()
                a.messageText = busy.count == 1 ? "Claude is still working on \(busy[0])." : "Claude is still working on \(busy.count) projects."
                a.informativeText = "Quitting stops Claude. Its progress so far is saved."
                a.addButton(withTitle: "Quit")
                a.addButton(withTitle: "Cancel")
                if a.runModal() != .alertFirstButtonReturn { NSApp.reply(toApplicationShouldTerminate: false); return }
            }
            self.quitting = true
            self.engine.stop { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    // ------------------------------------------------------------------ the app lock
    var lockEnabled: Bool { UserDefaults.standard.bool(forKey: "lockEnabled") }
    /// minutes of idling before it locks; 0 (unset) means 15, -1 never (still on launch and sleep)
    var lockMinutes: Int { let m = UserDefaults.standard.integer(forKey: "lockAfterMinutes"); return m == 0 ? 15 : m }

    func setupLock() {
        NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .leftMouseDown, .rightMouseDown, .scrollWheel]) { [weak self] e in
            self?.lastActivity = Date()
            return e
        }
        idleTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            guard let self = self, self.lockEnabled, !self.locked, self.lockMinutes > 0 else { return }
            if Date().timeIntervalSince(self.lastActivity) > Double(self.lockMinutes * 60) { self.lock(prompt: false) }
        }
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.screensDidSleepNotification, object: nil, queue: .main) { [weak self] _ in
            if self?.lockEnabled == true { self?.lock(prompt: false) }
        }
        DistributedNotificationCenter.default().addObserver(forName: NSNotification.Name("com.apple.screenIsLocked"), object: nil, queue: .main) { [weak self] _ in
            if self?.lockEnabled == true { self?.lock(prompt: false) }
        }
        if lockEnabled { lock() }
    }

    func lock(prompt: Bool = true) {
        if locked { return }
        locked = true
        let v = LockView(frame: container.bounds)
        v.autoresizingMask = [.width, .height]
        v.onUnlock = { [weak self] in self?.unlock() }
        container.addSubview(v)
        lockView = v
        web.isHidden = true
        if prompt { DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { self.unlock() } }
    }

    func authenticate(_ reason: String, _ done: @escaping (Bool) -> Void) {
        let ctx = LAContext()
        ctx.localizedCancelTitle = "Not Now"
        var e: NSError?
        guard ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &e) else { done(false); return }
        ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { ok, _ in DispatchQueue.main.async { done(ok) } }
    }

    @objc func unlock() {
        guard locked else { return }
        if !LAContext().canEvaluatePolicy(.deviceOwnerAuthentication, error: nil) { finishUnlock(); return }   // never lock anyone out
        authenticate("unlock your Tracewright projects") { ok in
            if ok { self.finishUnlock() } else { self.lockView?.failed() }
        }
    }

    func finishUnlock() {
        locked = false
        lastActivity = Date()
        web.isHidden = false
        let v = lockView
        lockView = nil
        NSAnimationContext.runAnimationGroup({ c in c.duration = 0.22; v?.animator().alphaValue = 0 }, completionHandler: { v?.removeFromSuperview() })
    }

    @objc func lockMenu() { if lockEnabled { lock() } else { command("settings", "security") } }

    // ------------------------------------------------------------------ menus
    func buildMenu() -> NSMenu {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            let holder = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            holder.submenu = m
            main.addItem(holder)
            return m
        }
        func cmd(_ title: String, _ name: String, _ key: String = "", _ mods: NSEvent.ModifierFlags = [.command], project: Bool = false) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: #selector(menuCommand(_:)), keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            i.representedObject = name
            i.target = self
            i.tag = project ? 1 : 0
            return i
        }
        func item(_ title: String, _ action: Selector?, _ key: String = "", _ mods: NSEvent.ModifierFlags = [.command], target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            if let t = target { i.target = t }
            return i
        }
        let sep = { NSMenuItem.separator() }

        let services = NSMenu(title: "Services")
        let servicesItem = item("Services", nil)
        servicesItem.submenu = services
        NSApp.servicesMenu = services
        _ = sub("Tracewright", [
            item("About Tracewright", #selector(about), target: self), sep(),
            cmd("Settings…", "settings", ","), cmd("Lessons", "lessons", "l", [.command, .shift]), sep(),
            item("Lock Tracewright", #selector(lockMenu), "l", [.command, .control], target: self), sep(),
            servicesItem, sep(),
            item("Hide Tracewright", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:))), sep(),
            item("Quit Tracewright", #selector(NSApplication.terminate(_:)), "q")])

        let kicad = NSMenu(title: "Open in KiCad")
        kicad.addItem(cmd("Board (PCB Editor)", "kicad-board", "b", [.command, .shift], project: true))
        kicad.addItem(cmd("Schematic (Schematic Editor)", "kicad-schematic", "e", [.command, .shift], project: true))
        kicad.addItem(cmd("Project (KiCad)", "kicad-project", project: true))
        let kicadItem = NSMenuItem(title: "Open in KiCad", action: nil, keyEquivalent: "")
        kicadItem.submenu = kicad
        _ = sub("File", [
            cmd("New Project…", "new-project", "n"), cmd("Import Project…", "import", "o"), cmd("Projects", "home", "p", [.command, .shift]), sep(),
            kicadItem, cmd("Save Checkpoint…", "checkpoint", "s", project: true), cmd("Download Project…", "download", project: true),
            cmd("Show in Finder", "reveal", "r", [.command, .option], project: true), sep(),
            item("Close Window", #selector(NSWindow.performClose(_:)), "w")])

        _ = sub("Edit", [
            item("Undo", Selector(("undo:")), "z"), item("Redo", Selector(("redo:")), "z", [.command, .shift]), sep(),
            item("Cut", #selector(NSText.cut(_:)), "x"), item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"), item("Select All", #selector(NSText.selectAll(_:)), "a"), sep(),
            cmd("Find…", "find", "f")])

        let tabs: [(String, String)] = [("Overview", "overview"), ("Board", "board"), ("Schematic", "schematic"), ("3D", "3d"),
                                        ("Bill of Materials", "bom"), ("Checks", "checks"), ("Order", "outputs"), ("Design Rules", "rules"),
                                        ("Brief & Docs", "docs"), ("Files", "files"), ("History", "history")]
        var view: [NSMenuItem] = tabs.enumerated().map { i, t in cmd(t.0, "tab:" + t.1, i < 9 ? String(i + 1) : i == 9 ? "0" : "", project: true) }
        view += [sep(), cmd("Review Flags", "review", "r", [.command, .shift], project: true),
                 cmd("Toggle Chat", "toggle-chat", "\\", project: true),
                 cmd("Mission Control", "mission", "m", [.command, .shift], project: true),
                 cmd("Timelapse", "timelapse", "t", [.command, .shift], project: true),
                 cmd("Calculators", "calculators", "c", [.command, .shift]),
                 cmd("Command Palette…", "palette", "k"), sep(),
                 item("Actual Size", #selector(zoomReset), "0", target: self), item("Zoom In", #selector(zoomIn), "=", target: self),
                 item("Zoom Out", #selector(zoomOut), "-", target: self), sep(),
                 item("Reload", #selector(reload), "r", target: self),
                 item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control])]
        _ = sub("View", view)

        _ = sub("Project", [
            cmd("Run All Checks", "run-checks", "k", [.command, .shift], project: true),
            cmd("Flag an Issue", "flag", "f", [.command, .shift], project: true),
            cmd("Send Flags to Claude", "send-flags", "\r", [.command, .shift], project: true), sep(),
            cmd("New Conversation", "new-chat", "n", [.command, .shift], project: true),
            cmd("Stop Claude", "stop", ".", project: true), sep(),
            cmd("Generate Outputs", "outputs", project: true), cmd("Render 3D Views", "render3d", project: true)])

        let win = sub("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"), item("Zoom", #selector(NSWindow.performZoom(_:))), sep(),
            item("Bring All to Front", #selector(NSApplication.arrangeInFront(_:)))])
        NSApp.windowsMenu = win

        let help = sub("Help", [
            cmd("Keyboard Shortcuts", "shortcuts", "/"), cmd("Release Notes", "whats-new"), cmd("Take the Tour", "tour"), sep(),
            item("Show Log", #selector(showLog), target: self), item("Open Data Folder", #selector(openData), target: self)])
        NSApp.helpMenu = help
        return main
    }

    @objc func menuCommand(_ sender: NSMenuItem) {
        guard !locked, let name = sender.representedObject as? String else { return }
        if !window.isVisible { window.makeKeyAndOrderFront(nil) }
        command(name)
    }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        if locked && (item.action == #selector(menuCommand(_:)) || item.action == #selector(reload)) { return false }
        if item.tag == 1 { return inProject && engine.port > 0 }
        if item.action == #selector(menuCommand(_:)) { return engine.port > 0 }
        return true
    }

    @objc func about() {
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "Tracewright", .applicationVersion: appVersion, .version: "",
            .credits: NSAttributedString(string: "KiCad boards, designed with Claude.",
                                         attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor])])
        NSApp.activate(ignoringOtherApps: true)
    }

    func setZoom(_ z: Double) {
        web.pageZoom = min(max(z, 0.6), 2.0)
        UserDefaults.standard.set(web.pageZoom, forKey: "pageZoom")
    }
    @objc func zoomReset() { setZoom(1) }
    @objc func zoomIn() { setZoom(web.pageZoom * 1.1) }
    @objc func zoomOut() { setZoom(web.pageZoom / 1.1) }
    @objc func reload() { if engine.port > 0 { web.reload() } }
    @objc func showLog() { NSWorkspace.shared.open(logURL) }
    @objc func openData() { NSWorkspace.shared.open(dataDir()) }
}

/// What the window shows while Tracewright is locked: the app's icon, a line, and the unlock button.
final class LockView: NSVisualEffectView {
    var onUnlock: (() -> Void)?
    private let note = NSTextField(labelWithString: "")

    override init(frame: NSRect) {
        super.init(frame: frame)
        material = .underWindowBackground
        blendingMode = .withinWindow
        state = .active
        let icon = NSImageView(image: NSApp.applicationIconImage ?? NSImage())
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.widthAnchor.constraint(equalToConstant: 88).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 88).isActive = true
        let title = NSTextField(labelWithString: "Tracewright is locked")
        title.font = .systemFont(ofSize: 20, weight: .semibold)
        let sub = NSTextField(labelWithString: "Unlock to continue.")
        sub.textColor = .secondaryLabelColor
        let bio = LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil)
        let button = NSButton(title: bio ? "Unlock with Touch ID" : "Unlock", target: self, action: #selector(tap))
        button.bezelStyle = .rounded
        button.controlSize = .large
        button.keyEquivalent = "\r"
        if #available(macOS 11.0, *) { button.image = NSImage(systemSymbolName: bio ? "touchid" : "lock.open", accessibilityDescription: nil); button.imagePosition = .imageLeading }
        note.textColor = .systemRed
        note.isHidden = true
        let stack = NSStackView(views: [icon, title, sub, button, note])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 10
        stack.setCustomSpacing(18, after: icon)
        stack.setCustomSpacing(22, after: sub)
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        stack.centerXAnchor.constraint(equalTo: centerXAnchor).isActive = true
        stack.centerYAnchor.constraint(equalTo: centerYAnchor, constant: -20).isActive = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    @objc func tap() { note.isHidden = true; onUnlock?() }

    func failed() { note.stringValue = "Not unlocked."; note.isHidden = false }

    // the window's title bar area still drags; everything else stops here
    override func mouseDown(with event: NSEvent) { if event.locationInWindow.y > bounds.height - 40 { window?.performDrag(with: event) } }
}

extension NSColor {
    convenience init?(hex: String) {
        var s = hex.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        self.init(srgbRed: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255, blue: CGFloat(v & 0xff) / 255, alpha: 1)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
