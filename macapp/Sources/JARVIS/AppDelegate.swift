import AppKit

/// Wires the pieces together: find the checkout (and set it up the first
/// time), start the backend, show the interface, and keep the menu-bar
/// item, the global shortcut and notifications in step with it. Quitting
/// stops the backend cleanly.
final class AppDelegate: NSObject, NSApplicationDelegate, StatusMenuActions {
    private var backend: Backend?
    private lazy var window = MainWindowController()
    private var status: StatusMenu?
    private var hotKey: HotKey?
    private let notifier = Notifier()
    private var setup: SetupWindowController?
    private let powerObserver = PowerObserver()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = MainMenu.build()
        status = StatusMenu(actions: self)
        notifier.setUp()
        notifier.onOpen = { [weak self] in self?.showJarvis() }
        hotKey = HotKey { [weak self] in self?.pushToTalk() }
        window.onPageMessage = { [weak self] message in self?.pageSaid(message) }
        powerObserver.onChange = { [weak self] thermalState, lowPowerMode in
            self?.backend?.reportPowerState(thermalState: thermalState, lowPowerMode: lowPowerMode)
        }
        powerObserver.start()
        window.showStarting()
        window.bringForward()
        boot()
    }

    /// Find the checkout (asking, the first time), set it up if it needs it,
    /// and start JARVIS.
    private func boot() {
        guard let root = Backend.locateRoot() ?? chooseCheckout() else {
            window.showProblem("JARVIS needs its folder",
                               detail: "Choose the folder you cloned JARVIS into (the one with scripts/start.sh), "
                                   + "from the menu bar item → Restart JARVIS.")
            return
        }
        let backend = Backend(root: root)
        self.backend = backend
        backend.onStateChange = { [weak self] state in self?.status?.update(state) }
        backend.onRestarted = { [weak self] in
            guard let self, let backend = self.backend else { return }
            self.window.load(backend.interfaceURL)
            self.notifier.post(title: "JARVIS restarted", body: "It stopped unexpectedly and has been started again.",
                               identifier: "restarted", sound: false)
        }
        if backend.isInstalled {
            startBackend()
        } else {
            let setup = SetupWindowController()
            self.setup = setup
            window.showStarting("Setting JARVIS up for the first time…")
            setup.run(in: root) { [weak self] ok in
                if ok {
                    self?.startBackend()
                } else {
                    self?.window.showProblem("Setup didn't finish",
                                             detail: "The setup window shows why. Fix it, then open JARVIS again.")
                }
            }
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let backend else { return .terminateNow }
        backend.stop { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }

    /// Clicking the Dock icon with the window closed brings it back.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showJarvis()
        return true
    }

    /// Closing the window leaves JARVIS listening in the menu bar.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    // MARK: - StatusMenuActions

    func showJarvis() {
        window.bringForward()
    }

    func pushToTalk() {
        window.bringForward()
        window.pushToTalk()
    }

    func restartJarvis() {
        guard let backend else {
            boot()                                  // never found its folder: ask again
            return
        }
        window.showStarting("Restarting JARVIS…")
        window.bringForward()
        backend.restart { [weak self] ok in self?.afterStart(ok) }
    }

    func openLogs() {
        NSWorkspace.shared.open(Backend.logURL)
    }

    // MARK: - Details

    private func startBackend() {
        guard let backend else { return }
        window.showStarting()
        backend.start()
        backend.waitUntilHealthy { [weak self] ok in self?.afterStart(ok) }
    }

    private func afterStart(_ ok: Bool) {
        guard let backend else { return }
        if ok {
            window.load(backend.interfaceURL)
            // A notification only fires on the next change; the Mac can
            // already be under thermal pressure or in Low Power Mode before
            // JARVIS ever starts, so report where things stand right now.
            powerObserver.report()
            return
        }
        var detail = "The log (menu bar item → Show Log) has the details."
        if case .failed(let reason) = backend.state {
            detail = reason + "\n\n" + detail
        }
        window.showProblem("JARVIS didn't start", detail: detail)
        window.bringForward()
    }

    /// The page's "worth telling you" moments become notifications — but
    /// only while you're looking at something else.
    private func pageSaid(_ message: [String: Any]) {
        guard !window.isFrontmost else { return }
        let kind = (message["kind"] as? String) ?? ""
        let title = (message["title"] as? String) ?? "JARVIS"
        let body = (message["body"] as? String) ?? ""
        let identifier = (message["id"] as? String).map { "\(kind)-\($0)" } ?? ""
        notifier.post(title: title, body: body, identifier: identifier, sound: kind == "confirm")
    }

    /// Ask once where the checkout is, and remember it.
    private func chooseCheckout() -> URL? {
        let panel = NSOpenPanel()
        panel.title = "Where is JARVIS?"
        panel.message = "Choose the folder you cloned JARVIS into — the one containing scripts/start.sh."
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else { return nil }
        guard Backend.isCheckout(url) else {
            let alert = NSAlert()
            alert.messageText = "That isn't a JARVIS folder"
            alert.informativeText = "It needs to contain scripts/start.sh and backend/jarvis."
            alert.runModal()
            return nil
        }
        UserDefaults.standard.set(url.path, forKey: Backend.rootDefaultsKey)
        return url
    }
}
