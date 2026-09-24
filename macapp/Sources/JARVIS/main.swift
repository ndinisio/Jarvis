import AppKit

// A regular app (Dock icon, windows) that also lives in the menu bar.
let application = NSApplication.shared
let appDelegate = AppDelegate()      // NSApplication holds its delegate weakly
application.delegate = appDelegate
application.setActivationPolicy(.regular)
application.run()
