import AppKit

/// First launch on a checkout that hasn't been set up: runs
/// `scripts/setup.sh` and shows what it's doing, instead of sending you to
/// Terminal. Calls back with whether it worked.
final class SetupWindowController: NSWindowController {
    private let output = NSTextView()
    private let status = NSTextField(labelWithString: "Setting JARVIS up — this takes a few minutes the first time.")
    private var process: Process?

    init() {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 460),
                              styleMask: [.titled, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Setting up JARVIS"
        window.backgroundColor = Theme.background
        window.isReleasedWhenClosed = false
        window.center()
        super.init(window: window)

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        output.isEditable = false
        output.drawsBackground = false
        output.textColor = NSColor(srgbRed: 0.54, green: 0.64, blue: 0.70, alpha: 1)
        output.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        output.autoresizingMask = [.width]
        scroll.documentView = output
        status.textColor = NSColor(srgbRed: 0.81, green: 0.89, blue: 0.93, alpha: 1)
        status.font = NSFont.systemFont(ofSize: 13, weight: .medium)

        let stack = NSStackView(views: [status, scroll])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 36, left: 20, bottom: 20, right: 20)
        scroll.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = stack
        NSLayoutConstraint.activate([
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -40),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 300),
        ])
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not used") }

    func run(in root: URL, completion: @escaping (Bool) -> Void) {
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [root.appendingPathComponent("scripts/setup.sh").path]
        process.currentDirectoryURL = root
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (environment["PATH"] ?? "/usr/bin:/bin")
        environment["NO_COLOR"] = "1"
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.append(text) }
        }
        process.terminationHandler = { [weak self] finished in
            pipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                let ok = finished.terminationStatus == 0
                self?.status.stringValue = ok ? "JARVIS is set up." :
                    "Setup didn't finish — the output above says why. Fix it, then open JARVIS again."
                if ok { self?.close() }
                completion(ok)
            }
        }
        do {
            try process.run()
            self.process = process
        } catch {
            status.stringValue = "Setup couldn't start: \(error.localizedDescription)"
            completion(false)
        }
    }

    private func append(_ text: String) {
        // setup.sh colours its output for a terminal; plain text here.
        let plain = text.replacingOccurrences(of: "\u{1B}\\[[0-9;]*m", with: "", options: .regularExpression)
        output.textStorage?.append(NSAttributedString(string: plain, attributes: [
            .font: output.font ?? NSFont.monospacedSystemFont(ofSize: 11, weight: .regular),
            .foregroundColor: output.textColor ?? NSColor.secondaryLabelColor,
        ]))
        output.scrollToEndOfDocument(nil)
    }
}
