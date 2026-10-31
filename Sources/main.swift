import Cocoa
@preconcurrency import WebKit

enum Connection: String, CaseIterable {
    case wifi = "Wi-Fi", usb = "USB", demo = "Demo"

    var arguments: [String] {
        switch self {
        case .wifi: return ["--wifi"]
        case .usb: return []
        case .demo: return ["--demo"]
        }
    }

    /// The name the MCP companion uses: wifi, usb or demo.
    var key: String {
        switch self {
        case .wifi: return "wifi"
        case .usb: return "usb"
        case .demo: return "demo"
        }
    }
}

func editorURL(from line: String) -> URL? {
    guard line.hasPrefix("Open: "), let url = URL(string: String(line.dropFirst(6))),
          url.scheme == "http", url.host == "127.0.0.1", url.port != nil,
          URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
            .contains(where: { $0.name == "token" && !($0.value ?? "").isEmpty }) == true
    else { return nil }
    return url
}

func token(of url: URL) -> String? {
    URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "token" })?.value
}

/// `rejig://connect?transport=wifi|usb|demo`, sent by the MCP companion's select_connection.
func connectTransport(from url: URL) -> Connection? {
    guard url.scheme == "rejig", url.host == "connect",
          let value = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
            .first(where: { $0.name == "transport" })?.value
    else { return nil }
    return Connection.allCases.first { $0.key == value }
}

/// Whether an editor `/api/job` answer says a read, apply or restore is running.
/// Is a job running, by `/api/job`'s answer? `nil` when the answer tells nothing (no answer,
/// another status, not the editor's JSON).
func jobRunning(in data: Data?, status: Int = 200) -> Bool? {
    guard status == 200, let data, let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          body.keys.contains("job") else { return nil }
    return (body["job"] as? [String: Any])?["state"] as? String == "running"
}

/// What `Rejig --approve` shows: `{"title": String, "lines": [String]}` on stdin.
func approvalRequest(from data: Data) -> (title: String, lines: [String])? {
    guard data.count <= 1_000_000,
          let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let title = body["title"] as? String, !title.isEmpty, title.count <= 300,
          let lines = body["lines"] as? [String], !lines.isEmpty, lines.count <= 2_000,
          lines.allSatisfy({ $0.count <= 400 && !$0.contains("\n") })
    else { return nil }
    return (title, lines)
}

if CommandLine.arguments.contains("--self-test") {
    precondition(editorURL(from: "Open: http://127.0.0.1:1234/?token=abc")?.port == 1234)
    precondition(editorURL(from: "Open: https://example.com/?token=abc") == nil)
    precondition(editorURL(from: "Open: http://127.0.0.1:1234/") == nil)
    precondition(editorURL(from: "Open: http://127.0.0.1.evil.test:1234/?token=abc") == nil)
    precondition(Connection.wifi.arguments == ["--wifi"])
    precondition(Connection.demo.arguments == ["--demo"])
    precondition(Connection.usb.arguments.isEmpty)
    precondition(connectTransport(from: URL(string: "rejig://connect?transport=demo")!) == .demo)
    precondition(connectTransport(from: URL(string: "rejig://connect?transport=wifi")!) == .wifi)
    precondition(connectTransport(from: URL(string: "rejig://connect?transport=Wi-Fi")!) == nil)
    precondition(connectTransport(from: URL(string: "rejig://apply?transport=usb")!) == nil)
    precondition(connectTransport(from: URL(string: "https://connect?transport=usb")!) == nil)
    precondition(jobRunning(in: Data(#"{"job":{"state":"running"}}"#.utf8)) == true)
    precondition(jobRunning(in: Data(#"{"job":{"state":"done"}}"#.utf8)) == false)
    precondition(jobRunning(in: Data(#"{"job":null}"#.utf8)) == false)
    precondition(jobRunning(in: nil) == nil && jobRunning(in: Data("{}".utf8)) == nil)
    precondition(jobRunning(in: Data(#"{"job":null}"#.utf8), status: 403) == nil)
    precondition(approvalRequest(from: Data(#"{"title":"Write?","lines":["1. Mail moves"]}"#.utf8))?.lines.count == 1)
    precondition(approvalRequest(from: Data(#"{"title":"Write?","lines":[]}"#.utf8)) == nil)
    precondition(approvalRequest(from: Data(#"{"title":"Write?","lines":["a\nb"]}"#.utf8)) == nil)
    precondition(approvalRequest(from: Data("not json".utf8)) == nil)
    precondition(token(of: URL(string: "http://127.0.0.1:1/?token=abc")!) == "abc")
    print("Rejig self-check passed")
    exit(0)
}

/// `Rejig --approve`: one window asking the person to approve an AI client's change list.
/// Exit 0 approve, 1 decline, 2 closed, timed out or unreadable input. Writes nothing.
@MainActor
final class Approval: NSObject, NSWindowDelegate {
    var panel: NSPanel!

    func show(title: String, lines: [String]) {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 620, height: 460),
            styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        panel.title = "Rejig: approve a Home Screen change"
        panel.level = .floating
        panel.delegate = self
        panel.isReleasedWhenClosed = false
        let heading = NSTextField(wrappingLabelWithString: title)
        heading.font = .boldSystemFont(ofSize: 14)
        let note = NSTextField(wrappingLabelWithString:
            "An AI assistant asked to make these changes on your iPhone. Approve only if you asked for them.")
        note.textColor = .secondaryLabelColor
        let list = NSTextView()
        list.isEditable = false
        list.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        list.string = lines.joined(separator: "\n")
        list.setAccessibilityLabel("Changes")
        let scroll = NSScrollView()
        scroll.documentView = list
        scroll.hasVerticalScroller = true
        list.autoresizingMask = [.width]
        list.isVerticallyResizable = true
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        // Escape declines; Return does nothing, so approving always takes a deliberate click.
        let decline = NSButton(title: "Decline", target: self, action: #selector(declined))
        decline.keyEquivalent = "\u{1b}"
        let approve = NSButton(title: "Approve", target: self, action: #selector(approved))
        let buttons = NSStackView(views: [decline, approve])
        let stack = NSStackView(views: [heading, note, scroll, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        scroll.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -32).isActive = true
        panel.contentView = stack
        panel.center()
        NSApp.activate(ignoringOtherApps: true)
        panel.orderFrontRegardless()
        panel.makeKey()
        DispatchQueue.main.asyncAfter(deadline: .now() + 600) { exit(2) }
    }

    @objc func approved() { exit(0) }
    @objc func declined() { exit(1) }
    func windowWillClose(_ notification: Notification) { exit(2) }
}

if CommandLine.arguments.contains("--approve") {
    guard let request = approvalRequest(from: FileHandle.standardInput.readDataToEndOfFile()) else { exit(2) }
    let approval = Approval()
    NSApplication.shared.setActivationPolicy(.accessory)
    approval.show(title: request.title, lines: request.lines)
    NSApplication.shared.run()
    exit(2)
}

final class Answer: @unchecked Sendable { var running: Bool? }

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
    WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
    let support = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Rejig", isDirectory: true)
    let smokeTest = CommandLine.arguments.contains("--smoke-test")
    var window: NSWindow!
    var webView: WKWebView!
    var picker: NSPopUpButton!
    var pairButton: NSButton!
    var status: NSTextField!
    var backend: Process?
    var pairing: Process?
    var output = ""
    var pageURL: URL?
    var mode = Connection.wifi
    var busy = false
    var dirty = false
    var transitioning = false
    var quitting = false
    var receivedEditorState = false

    var python: URL { support.appendingPathComponent(".venv/bin/python3") }
    /// Tells the MCP companion how to reach this editor; only while the editor runs.
    var bridgeFile: URL { support.appendingPathComponent("mcp-bridge.json") }

    func writeBridge() {
        guard let url = pageURL, let port = url.port, let secret = token(of: url), let process = backend,
              let data = try? JSONSerialization.data(withJSONObject: [
                "port": port, "token": secret, "pid": Int(process.processIdentifier),
                "connection": mode.key, "dirty": dirty,
              ], options: [.sortedKeys])
        else { return }
        let temporary = support.appendingPathComponent(".mcp-bridge.json.tmp")
        try? FileManager.default.removeItem(at: temporary)
        guard FileManager.default.createFile(atPath: temporary.path, contents: data,
                                             attributes: [.posixPermissions: 0o600]) else { return }
        rename(temporary.path, bridgeFile.path)
    }

    func removeBridge() { try? FileManager.default.removeItem(at: bridgeFile) }

    /// Is the editor running a job, whoever started it? The page only knows its own jobs;
    /// an AI client's read or write runs in the same editor. `nil` when the editor does not
    /// answer in time or answers oddly: it may be mid-write. One refusing the connection runs
    /// nothing.
    func editorBusy() -> Bool? {
        guard backend != nil, let url = pageURL, let port = url.port, let secret = token(of: url),
              let job = URL(string: "http://127.0.0.1:\(port)/api/job") else { return false }
        var request = URLRequest(url: job, timeoutInterval: 2)
        request.setValue("ihs_\(port)=\(secret)", forHTTPHeaderField: "Cookie")
        request.setValue(secret, forHTTPHeaderField: "X-IHS-Token")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.connectionProxyDictionary = [:]
        let answer = Answer()
        let done = DispatchSemaphore(value: 0)
        URLSession(configuration: configuration).dataTask(with: request) { data, response, error in
            answer.running = (error as? URLError)?.code == .cannotConnectToHost
                ? false : jobRunning(in: data, status: (response as? HTTPURLResponse)?.statusCode ?? 0)
            done.signal()
        }.resume()
        guard done.wait(timeout: .now() + 3) == .success else { return nil }
        return answer.running
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700])
        } catch { fatalError("Cannot create app data directory: \(error)") }
        mode = smokeTest ? .demo : Connection(rawValue: UserDefaults.standard.string(forKey: "connection") ?? "") ?? .wifi
        makeMenu()
        makeWindow()
        startEditor()
        NSApp.activate(ignoringOtherApps: true)
    }

    func makeMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Rejig", action: #selector(about), keyEquivalent: "")
        appMenu.addItem(withTitle: "Show Backups", action: #selector(showBackups), keyEquivalent: "b")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Rejig", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        NSApp.mainMenu = main
    }

    func makeWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1380, height: 900),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Rejig"
        window.minSize = NSSize(width: 1050, height: 700)
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.center()

        picker = NSPopUpButton()
        picker.addItems(withTitles: Connection.allCases.map(\.rawValue))
        picker.selectItem(withTitle: mode.rawValue)
        picker.target = self
        picker.action = #selector(changeConnection)
        picker.setAccessibilityLabel("Connection")
        pairButton = NSButton(title: "Set Up Wi-Fi…", target: self, action: #selector(setUpWifi))
        let reload = NSButton(title: "Reconnect", target: self, action: #selector(reconnect))
        status = NSTextField(labelWithString: "Starting…")
        status.textColor = .secondaryLabelColor
        let controls = NSStackView(views: [picker, pairButton, reload, status])
        controls.spacing = 12
        controls.edgeInsets = NSEdgeInsets(top: 10, left: 16, bottom: 10, right: 16)
        controls.setContentHuggingPriority(.required, for: .vertical)

        let content = WKUserContentController()
        content.add(self, name: "editorState")
        content.add(self, name: "smokeResult")
        let configuration = WKWebViewConfiguration()
        configuration.userContentController = content
        configuration.websiteDataStore = .nonPersistent()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        let stack = NSStackView(views: [controls, webView])
        stack.orientation = .vertical
        stack.alignment = .width
        stack.spacing = 0
        window.contentView = stack
        window.makeKeyAndOrderFront(nil)
    }

    let bridgeScript = """
        void import('/static/js/store.js').then(({getState, subscribe}) => {
            const report = () => {
                const state = getState();
                window.webkit.messageHandlers.editorState.postMessage({
                    busy: state.job?.state === 'running',
                    dirty: !!(state.check?.dirty || state.check?.pending || state.removed?.length)
                });
                setTimeout(() => {
                    const name = document.querySelector('.brand-name');
                    if (name) name.textContent = 'Rejig';
                }, 0);
            };
            subscribe(report); report();
        });
        """

    func alert(_ title: String, _ message: String) {
        let panel = NSAlert()
        panel.messageText = title
        panel.informativeText = message
        panel.runModal()
    }

    func canLeaveEditor() -> Bool {
        let working = transitioning || pairing != nil || busy ? true : editorBusy()
        if working == true {
            alert("Please wait for the current operation", "Let reading, applying, verification, or rollback finish before closing or reconnecting.")
            return false
        }
        if working == nil {
            let panel = NSAlert()
            panel.messageText = "Rejig's editor is not answering"
            panel.informativeText = "An AI client may be writing to your iPhone through it, and leaving now could interrupt that write. Its backup stays in Show Backups."
            panel.addButton(withTitle: "Wait")
            panel.addButton(withTitle: "Leave Anyway")
            guard panel.runModal() == .alertSecondButtonReturn else { return false }
        }
        guard dirty else { return true }
        let panel = NSAlert()
        panel.messageText = "Discard your layout edits?"
        panel.informativeText = "Apply or export the layout to keep your changes."
        panel.addButton(withTitle: "Keep Editing")
        panel.addButton(withTitle: "Discard Edits")
        return panel.runModal() == .alertSecondButtonReturn
    }

    func startEditor() {
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            status.stringValue = "Setup needed"
            alert("Rejig needs its local runtime", "Run scripts/install.sh from the source project on this Mac, then reopen the app.")
            return
        }
        busy = false
        dirty = false
        receivedEditorState = false
        pageURL = nil
        removeBridge()
        output = ""
        status.stringValue = mode == .demo ? "Demo · no phone contacted" : "\(mode.rawValue) · unlock your iPhone"
        let process = Process()
        process.executableURL = python
        process.arguments = ["-u", "-m", "ios_homescreen", "editor", "--no-browser",
            "--data-dir", support.appendingPathComponent("data").path] + mode.arguments
        // Keep cached icons (an installed-app inventory) with the rest of Rejig's data.
        process.environment = ProcessInfo.processInfo.environment.merging([
            "PYTHONUNBUFFERED": "1",
            "IOS_HOMESCREEN_CACHE_DIR": support.appendingPathComponent("cache").path,
        ]) { _, new in new }
        let pipe = Pipe()
        process.standardOutput = pipe
        let logURL = support.appendingPathComponent("backend.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
        process.standardError = try? FileHandle(forWritingTo: logURL)
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            let text = String(decoding: data, as: UTF8.self)
            Task { @MainActor in
                guard let self, self.backend === process else { return }
                self.output += text
                while let end = self.output.firstIndex(of: "\n") {
                    let line = String(self.output[..<end])
                    self.output.removeSubrange(...end)
                    if let url = editorURL(from: line) {
                        self.pageURL = url
                        self.writeBridge()
                        self.webView.load(URLRequest(url: url))
                    }
                }
            }
        }
        process.terminationHandler = { [weak self] stopped in
            Task { @MainActor in
                guard let self, self.backend === stopped else { return }
                self.backend = nil
                self.removeBridge()
                self.busy = false
                self.status.stringValue = "Disconnected"
                if !self.quitting {
                    self.alert("The connection service stopped", "Reconnect to try again. Details: \(logURL.path)")
                }
            }
        }
        backend = process
        do { try process.run() }
        catch {
            backend = nil
            alert("Could not start the connection service", error.localizedDescription)
        }
    }

    func stopEditor(_ done: @escaping @MainActor () -> Void) {
        removeBridge()
        guard let process = backend else { done(); return }
        backend = nil
        process.terminationHandler = nil
        transitioning = true
        status.stringValue = "Disconnecting…"
        if process.isRunning { process.terminate() }
        DispatchQueue.global().async {
            process.waitUntilExit()
            Task { @MainActor in self.transitioning = false; done() }
        }
    }

    @objc func changeConnection() {
        guard canLeaveEditor() else { picker.selectItem(withTitle: mode.rawValue); return }
        mode = Connection(rawValue: picker.titleOfSelectedItem ?? "") ?? .wifi
        UserDefaults.standard.set(mode.rawValue, forKey: "connection")
        stopEditor { self.startEditor() }
    }

    /// `rejig://connect?transport=…` from the MCP companion: switch only when nothing would
    /// be interrupted or lost; otherwise say so here and leave it (no dialog).
    func application(_ application: NSApplication, open urls: [URL]) {
        guard let target = urls.lazy.compactMap(connectTransport(from:)).first, target != mode else { return }
        if transitioning || pairing != nil || busy || dirty || editorBusy() != false {
            status.stringValue = "An AI client asked for \(target.rawValue); finish the current work first"
            return
        }
        mode = target
        picker.selectItem(withTitle: mode.rawValue)
        UserDefaults.standard.set(mode.rawValue, forKey: "connection")
        stopEditor { self.startEditor() }
    }

    @objc func reconnect() {
        guard canLeaveEditor() else { return }
        stopEditor { self.startEditor() }
    }

    @objc func setUpWifi() {
        guard canLeaveEditor() else { return }
        let panel = NSAlert()
        panel.messageText = "Set up Wi-Fi access"
        panel.informativeText = "Connect your iPhone to this Mac with USB, unlock it, and tap Trust if asked. This pairs this Mac and enables wireless device connections. Afterwards, keep both devices on the same network."
        panel.addButton(withTitle: "Pair Connected iPhone")
        panel.addButton(withTitle: "Cancel")
        guard panel.runModal() == .alertFirstButtonReturn else { return }
        stopEditor { self.pairWifi() }
    }

    func pairWifi() {
        let process = Process()
        process.executableURL = python
        process.arguments = ["-u", "-m", "ios_homescreen", "wifi", "setup", "--json"]
        let logURL = support.appendingPathComponent("pairing.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
        let log = try? FileHandle(forWritingTo: logURL)
        process.standardOutput = log
        process.standardError = log
        pairing = process
        pairButton.isEnabled = false
        picker.isEnabled = false
        status.stringValue = "Pairing over USB…"
        process.terminationHandler = { [weak self] stopped in
            Task { @MainActor in
                guard let self else { return }
                self.pairing = nil
                self.pairButton.isEnabled = true
                self.picker.isEnabled = true
                if stopped.terminationStatus == 0 {
                    self.mode = .wifi
                    self.picker.selectItem(withTitle: self.mode.rawValue)
                    UserDefaults.standard.set(self.mode.rawValue, forKey: "connection")
                    self.alert("Wi-Fi pairing saved", "You can unplug the cable. Keep the iPhone awake and on the same network, then choose Read from phone.")
                } else {
                    self.alert("Wi-Fi setup did not finish", "Check the USB cable, unlock the iPhone and accept Trust. Details: \(logURL.path)")
                }
                self.startEditor()
            }
        }
        do { try process.run() }
        catch {
            pairing = nil
            pairButton.isEnabled = true
            picker.isEnabled = true
            alert("Could not start pairing", error.localizedDescription)
            startEditor()
        }
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, message.frameInfo.request.url?.port == pageURL?.port else { return }
        if message.name == "smokeResult", smokeTest {
            let result = message.body as? [String: Any] ?? [:]
            print("SMOKE_RESULT \(message.body)")
            fflush(stdout)
            quitting = true
            let succeeded = result["ok"] as? Bool == true && receivedEditorState
            print("NATIVE_STATE_BRIDGE \(receivedEditorState ? "passed" : "FAILED")")
            fflush(stdout)
            webView.takeSnapshot(with: nil) { image, _ in
                if let tiff = image?.tiffRepresentation,
                   let bitmap = NSBitmapImageRep(data: tiff),
                   let png = bitmap.representation(using: .png, properties: [:]) {
                    try? png.write(to: self.support.appendingPathComponent("demo-preview.png"))
                }
                self.stopEditor { exit(succeeded ? 0 : 1) }
            }
        } else if backend != nil, let state = message.body as? [String: Bool] {
            receivedEditorState = true
            busy = state["busy"] ?? false
            let wasDirty = dirty
            dirty = state["dirty"] ?? false
            if dirty != wasDirty { writeBridge() }
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        webView.evaluateJavaScript(bridgeScript)
        guard smokeTest, let resource = Bundle.main.url(forResource: "smoke", withExtension: "js"),
              let script = try? String(contentsOf: resource, encoding: .utf8) else { return }
        webView.evaluateJavaScript(script) { _, error in
            if let error { print("SMOKE_SCRIPT_ERROR \(error)"); exit(1) }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
        let url = action.request.url
        if action.shouldPerformDownload { decisionHandler(.download) }
        else if url?.scheme == "about" || (url?.scheme == "http" && url?.host == "127.0.0.1" && url?.port == pageURL?.port) {
            decisionHandler(.allow)
        } else { decisionHandler(.cancel) }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping @MainActor @Sendable (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.beginSheetModal(for: window) { result in
            completionHandler(result == .OK ? panel.url : nil)
        }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.beginSheetModal(for: window) { result in completionHandler(result == .OK ? panel.urls : nil) }
    }

    @objc func showBackups() {
        let directory = support.appendingPathComponent("data/backups", isDirectory: true)
        do { try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true) }
        catch { alert("Cannot open backups", error.localizedDescription); return }
        NSWorkspace.shared.open(directory)
    }

    @objc func about() {
        alert("Rejig 0.1", "Arrange your iPhone Home Screen over USB or Wi-Fi.\n\nMIT license, provided without warranty. Powered by ios-homescreen (MIT), by Yakup Emre Yerli, and pymobiledevice3 (GPL-3.0-or-later). Not affiliated with Apple.\n\nLayouts and backups stay on this Mac. Wi-Fi needs one-time USB pairing. Your iOS version must be tested on your phone.")
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool { NSApp.terminate(self); return false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard canLeaveEditor() else { return .terminateCancel }
        quitting = true
        guard backend != nil else { return .terminateNow }
        stopEditor { sender.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.setActivationPolicy(.regular)
app.delegate = delegate
app.run()
