import AppKit
import WebKit

/// The JARVIS interface in a window of its own.
///
/// It's the same interface the browser tab shows, loaded from the local
/// backend with this session's token. The page is told it's in the app
/// (`window.__JARVIS_NATIVE__`) and can send it messages — "JARVIS needs
/// your OK", "a task finished" — through the `jarvis` handler. Closing the
/// window only hides it: JARVIS keeps listening from the menu bar.
final class MainWindowController: NSWindowController, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let webView: WKWebView
    /// Messages from the page (see frontend/src/lib/native.ts).
    var onPageMessage: (([String: Any]) -> Void)?

    init() {
        let contentController = WKUserContentController()
        contentController.addUserScript(WKUserScript(
            source: "window.__JARVIS_NATIVE__ = true;",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true))
        let configuration = WKWebViewConfiguration()
        configuration.userContentController = contentController
        configuration.mediaTypesRequiringUserActionForPlayback = []
        let view = WKWebView(frame: .zero, configuration: configuration)
        if #available(macOS 13.3, *) {
            view.isInspectable = true              // Safari → Develop, for the curious
        }
        view.underPageBackgroundColor = Theme.background
        webView = view

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "JARVIS"
        window.titlebarAppearsTransparent = true
        window.backgroundColor = Theme.background
        window.minSize = NSSize(width: 900, height: 600)
        window.contentView = view
        window.isReleasedWhenClosed = false
        window.center()
        window.setFrameAutosaveName("JARVISMainWindow")
        super.init(window: window)

        window.delegate = self
        view.navigationDelegate = self
        view.uiDelegate = self
        contentController.add(MessageRelay(owner: self), name: "jarvis")
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not used") }

    // MARK: - Content

    func showStarting(_ message: String = "Starting JARVIS…") {
        webView.loadHTMLString(Theme.page(title: message, detail: ""), baseURL: nil)
    }

    func showProblem(_ title: String, detail: String) {
        webView.loadHTMLString(Theme.page(title: title, detail: detail), baseURL: nil)
    }

    func load(_ url: URL) {
        webView.load(URLRequest(url: url))
    }

    /// The global shortcut: start (or stop) push-to-talk in the page.
    func pushToTalk() {
        webView.evaluateJavaScript("window.jarvisNative && window.jarvisNative.pushToTalk && window.jarvisNative.pushToTalk()")
    }

    var isFrontmost: Bool {
        NSApp.isActive && (window?.isKeyWindow ?? false)
    }

    func bringForward() {
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - NSWindowDelegate

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)                       // hide; JARVIS carries on in the menu bar
        return false
    }

    // MARK: - WKNavigationDelegate

    /// Links to anywhere but JARVIS itself open in your browser.
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url, navigationAction.navigationType == .linkActivated,
           !isJarvis(url) {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // MARK: - WKUIDelegate

    /// target="_blank" links: your browser, not a second web view.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    /// Push-to-talk records in the page: the microphone is JARVIS's own page's
    /// to use, and nobody else's.
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(origin.host == "127.0.0.1" && type == .microphone ? .grant : .deny)
    }

    private func isJarvis(_ url: URL) -> Bool {
        url.host == "127.0.0.1" || url.scheme == "about"
    }

    fileprivate func received(_ message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, let body = message.body as? [String: Any] else { return }
        onPageMessage?(body)
    }
}

/// WKUserContentController keeps its handlers strongly; this breaks the cycle.
private final class MessageRelay: NSObject, WKScriptMessageHandler {
    weak var owner: MainWindowController?

    init(owner: MainWindowController) {
        self.owner = owner
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        owner?.received(message)
    }
}

/// The interface's colours, so the window never flashes white while loading.
enum Theme {
    static let background = NSColor(srgbRed: 0.027, green: 0.043, blue: 0.067, alpha: 1)

    static func page(title: String, detail: String) -> String {
        """
        <!doctype html><html><head><meta charset="utf-8"><style>
        html,body{margin:0;height:100%;background:#070b11;color:#cfe3ee;
          font:15px -apple-system,BlinkMacSystemFont,sans-serif;display:flex;align-items:center;justify-content:center}
        main{text-align:center;max-width:560px;padding:24px}
        .dot{width:14px;height:14px;border-radius:50%;background:#5fd3f3;margin:0 auto 18px;
          box-shadow:0 0 18px #5fd3f3;animation:p 1.6s ease-in-out infinite}
        @keyframes p{50%{opacity:.35}}
        h1{font-weight:500;font-size:17px;letter-spacing:.02em;margin:0 0 10px}
        pre{white-space:pre-wrap;color:#8aa3b3;font:12px ui-monospace,Menlo,monospace;text-align:left}
        </style></head><body><main><div class="dot"></div><h1>\(escape(title))</h1>
        <pre>\(escape(detail))</pre></main></body></html>
        """
    }

    private static func escape(_ text: String) -> String {
        text.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
    }
}
