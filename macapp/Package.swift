// swift-tools-version:5.9
//
// JARVIS for the Mac: a native shell around the JARVIS you already installed.
// It starts the backend from your checkout's .venv (exactly as scripts/start.sh
// does), shows the interface in its own window, and adds what a browser tab
// can't: a menu-bar item, a global push-to-talk shortcut, notifications when
// JARVIS needs you, and Launch at Login.  Build with ./build.sh.
import PackageDescription

let package = Package(
    name: "JARVIS",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "JARVIS",
            path: "Sources/JARVIS",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("WebKit"),
                .linkedFramework("Carbon"),
                .linkedFramework("UserNotifications"),
                .linkedFramework("ServiceManagement"),
            ]
        ),
    ]
)
