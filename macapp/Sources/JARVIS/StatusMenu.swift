import AppKit
import ServiceManagement

/// What the menu bar item and the app menu can ask the app to do.
protocol StatusMenuActions: AnyObject {
    func showJarvis()
    func pushToTalk()
    func restartJarvis()
    func openLogs()
}

/// JARVIS in the menu bar: always one click away, even with its window
/// closed — show it, talk to it, start it at login, restart it, or quit.
final class StatusMenu: NSObject, NSMenuDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private weak var actions: StatusMenuActions?
    private let loginItem = NSMenuItem(title: "Open at Login", action: #selector(StatusMenu.toggleLogin),
                                       keyEquivalent: "")
    private let stateItem = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")

    init(actions: StatusMenuActions) {
        self.actions = actions
        super.init()
        if let button = item.button {
            let image = NSImage(systemSymbolName: "waveform.circle", accessibilityDescription: "JARVIS")
            image?.isTemplate = true
            button.image = image
            button.toolTip = "JARVIS"
        }
        let menu = NSMenu()
        menu.delegate = self
        stateItem.isEnabled = false
        menu.addItem(stateItem)
        menu.addItem(.separator())
        menu.addItem(entry("Show JARVIS", #selector(show), key: ""))
        let talk = entry("Push to Talk", #selector(talk), key: " ")
        talk.keyEquivalentModifierMask = [.option]
        menu.addItem(talk)
        menu.addItem(.separator())
        loginItem.target = self
        menu.addItem(loginItem)
        menu.addItem(entry("Restart JARVIS", #selector(restart), key: ""))
        menu.addItem(entry("Show Log", #selector(logs), key: ""))
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit JARVIS", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
        item.menu = menu
    }

    /// One line at the top of the menu saying how JARVIS is.
    func update(_ state: Backend.State) {
        switch state {
        case .stopped: stateItem.title = "Stopped"
        case .starting: stateItem.title = "Starting…"
        case .running: stateItem.title = "Running"
        case .failed: stateItem.title = "Needs attention — see the window"
        }
    }

    func menuWillOpen(_ menu: NSMenu) {
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
    }

    private func entry(_ title: String, _ selector: Selector, key: String) -> NSMenuItem {
        let menuItem = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        menuItem.target = self
        return menuItem
    }

    @objc private func show() { actions?.showJarvis() }
    @objc private func talk() { actions?.pushToTalk() }
    @objc private func restart() { actions?.restartJarvis() }
    @objc private func logs() { actions?.openLogs() }

    @objc private func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            let alert = NSAlert()
            alert.messageText = "JARVIS couldn't change its login setting"
            alert.informativeText = "\(error.localizedDescription)\n\nYou can add it yourself in System Settings → "
                + "General → Login Items."
            alert.runModal()
        }
    }
}

/// The app's own menu bar: the standard App, Edit and Window menus — the
/// Edit menu is what makes ⌘C, ⌘V and ⌘A work inside the interface.
enum MainMenu {
    static func build() -> NSMenu {
        let bar = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About JARVIS", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide JARVIS", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "Hide Others",
                                         action: #selector(NSApplication.hideOtherApplications(_:)),
                                         keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit JARVIS", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        bar.addItem(submenu(appMenu, title: "JARVIS"))

        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        bar.addItem(submenu(edit, title: "Edit"))

        let window = NSMenu(title: "Window")
        window.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        window.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        window.addItem(.separator())
        // WKWebView's own reload(_:), reached through the responder chain.
        window.addItem(withTitle: "Reload Interface", action: Selector(("reload:")), keyEquivalent: "r")
        bar.addItem(submenu(window, title: "Window"))
        NSApp.windowsMenu = window
        return bar
    }

    private static func submenu(_ menu: NSMenu, title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.submenu = menu
        return item
    }
}
