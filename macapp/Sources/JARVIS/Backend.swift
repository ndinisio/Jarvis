import Foundation
import Security

/// The JARVIS backend, run from your checkout exactly as `scripts/start.sh`
/// runs it — so the interface is rebuilt after a `git pull` and nothing is
/// frozen into the app. The app owns the process: it starts it with this
/// session's token on a free port, waits for `/api/health`, restarts it once
/// if it stops unexpectedly, and stops it cleanly (SIGTERM, then SIGKILL)
/// when you quit.
final class Backend {
    enum State: Equatable {
        case stopped
        case starting
        case running
        case failed(String)
    }

    let root: URL
    /// This session's token: the page carries it, so only this window can
    /// drive JARVIS (see backend/jarvis/core/auth.py).
    let token: String
    private(set) var port: Int = 0
    private(set) var state: State = .stopped {
        didSet { if state != oldValue { onStateChange?(state) } }
    }
    var onStateChange: ((State) -> Void)?
    /// Called after an unexpected exit was recovered by a restart.
    var onRestarted: (() -> Void)?

    private var process: Process?
    private var stopping = false
    private var stopCompletion: (() -> Void)?
    private var recentRestarts: [Date] = []
    private var log: FileHandle?

    init(root: URL) {
        self.root = root
        self.token = Backend.makeToken()
    }

    // MARK: - Finding the checkout

    static let rootDefaultsKey = "JARVISCheckout"

    /// Your JARVIS checkout: the one you chose before, the one this app was
    /// built inside, or a usual place.
    static func locateRoot() -> URL? {
        let fileManager = FileManager.default
        var candidates: [URL] = []
        if let saved = UserDefaults.standard.string(forKey: rootDefaultsKey) {
            candidates.append(URL(fileURLWithPath: saved))
        }
        var folder = Bundle.main.bundleURL.deletingLastPathComponent()
        for _ in 0..<5 {
            candidates.append(folder)
            folder = folder.deletingLastPathComponent()
        }
        let home = fileManager.homeDirectoryForCurrentUser
        for name in ["Jarvis", "JARVIS-app", "jarvis", "Developer/Jarvis", "Projects/Jarvis", "src/Jarvis"] {
            candidates.append(home.appendingPathComponent(name))
        }
        return candidates.first(where: isCheckout)
    }

    static func isCheckout(_ url: URL) -> Bool {
        let fileManager = FileManager.default
        return fileManager.fileExists(atPath: url.appendingPathComponent("scripts/start.sh").path)
            && fileManager.fileExists(atPath: url.appendingPathComponent("backend/jarvis").path)
    }

    /// Set up already (scripts/setup.sh has made the virtual environment)?
    var isInstalled: Bool {
        FileManager.default.isExecutableFile(atPath: root.appendingPathComponent(".venv/bin/jarvis").path)
    }

    var interfaceURL: URL {
        URL(string: "http://127.0.0.1:\(port)/?token=\(token)")!
    }

    static var logURL: URL {
        let folder = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/JARVIS", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder.appendingPathComponent("backend.log")
    }

    // MARK: - Running

    func start() {
        guard process == nil else { return }
        stopping = false
        if port == 0 { port = Backend.freePort() ?? 8765 }
        state = .starting

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [root.appendingPathComponent("scripts/start.sh").path,
                             "--no-browser", "--port", String(port)]
        process.currentDirectoryURL = root
        process.environment = environment()
        if let log = openLog() {
            process.standardOutput = log
            process.standardError = log
        }
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async { self?.exited(finished) }
        }
        do {
            try process.run()
            self.process = process
        } catch {
            state = .failed("JARVIS couldn't be started: \(error.localizedDescription)")
        }
    }

    /// Poll the health check until it answers (then `.running`), the process
    /// dies, or *timeout* passes. The first start after a pull can include an
    /// interface rebuild, hence the generous default.
    func waitUntilHealthy(timeout: TimeInterval = 240, completion: @escaping (Bool) -> Void) {
        let deadline = Date().addingTimeInterval(timeout)
        let url = URL(string: "http://127.0.0.1:\(port)/api/health")!

        func poll() {
            guard process != nil, !stopping else {
                completion(false)
                return
            }
            var request = URLRequest(url: url)
            request.timeoutInterval = 2
            URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
                DispatchQueue.main.async {
                    guard let self else { return }
                    if (response as? HTTPURLResponse)?.statusCode == 200 {
                        self.state = .running
                        completion(true)
                    } else if Date() > deadline {
                        self.state = .failed("JARVIS didn't start within \(Int(timeout)) seconds.")
                        completion(false)
                    } else {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { poll() }
                    }
                }
            }.resume()
        }
        poll()
    }

    /// Stop gracefully: SIGTERM (uvicorn shuts JARVIS down cleanly), and
    /// SIGKILL only if it hasn't gone after five seconds.
    func stop(completion: @escaping () -> Void) {
        guard let process, process.isRunning else {
            state = .stopped
            completion()
            return
        }
        stopping = true
        stopCompletion = completion
        process.terminate()
        let pid = process.processIdentifier
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            if self?.process?.isRunning == true {
                kill(pid, SIGKILL)
            }
        }
    }

    func restart(completion: @escaping (Bool) -> Void) {
        stop { [weak self] in
            guard let self else { return }
            self.start()
            self.waitUntilHealthy(completion: completion)
        }
    }

    /// Tell the backend about a thermal/Low Power Mode change (see
    /// core/power.py) — best-effort, fire-and-forget: if this request is
    /// lost, the next change (or the one PowerObserver sends right after
    /// startup) reports the current state anyway, so nothing needs a retry.
    func reportPowerState(thermalState: String, lowPowerMode: Bool) {
        guard state == .running,
              let body = try? JSONSerialization.data(withJSONObject: [
                  "thermal_state": thermalState, "low_power_mode": lowPowerMode,
              ])
        else { return }
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/api/system/power-state")!)
        request.httpMethod = "POST"
        request.setValue(token, forHTTPHeaderField: "x-jarvis-token")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        URLSession.shared.dataTask(with: request).resume()
    }

    // MARK: - Details

    private func exited(_ finished: Process) {
        process = nil
        try? log?.close()
        log = nil
        if stopping {
            state = .stopped
            stopCompletion?()
            stopCompletion = nil
            return
        }
        let status = finished.terminationStatus
        if state == .starting {
            // It never came up: most often "JARVIS is already running" from a
            // terminal, or a broken setup. Say what the log says.
            state = .failed(Backend.lastLines() ?? "JARVIS stopped while starting (exit \(status)).")
            return
        }
        // It was running and stopped by itself: one restart, then give up
        // rather than loop.
        recentRestarts = recentRestarts.filter { $0.timeIntervalSinceNow > -300 }
        guard recentRestarts.count < 2 else {
            state = .failed("JARVIS stopped unexpectedly more than once. The log has the details.")
            return
        }
        recentRestarts.append(Date())
        let delay = recentRestarts.count == 1 ? 1.5 : 5.0
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self else { return }
            self.start()
            self.waitUntilHealthy { ok in
                if ok { self.onRestarted?() }
            }
        }
    }

    private func environment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        environment["JARVIS_SESSION_TOKEN"] = token
        // An app opened from Finder gets a bare PATH; Homebrew's node and
        // ollama live outside it.
        let path = environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + path
        environment["PYTHONUNBUFFERED"] = "1"
        return environment
    }

    private func openLog() -> FileHandle? {
        let url = Backend.logURL
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: url) else { return nil }
        handle.seekToEndOfFile()
        let banner = "\n--- JARVIS app starting the backend, \(Date()) ---\n"
        handle.write(banner.data(using: .utf8)!)
        log = handle
        return handle
    }

    static func lastLines(_ count: Int = 3) -> String? {
        guard let text = try? String(contentsOf: logURL, encoding: .utf8) else { return nil }
        let lines = text.split(separator: "\n").map(String.init)
            .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        let tail = lines.suffix(count).joined(separator: "\n")
        return tail.isEmpty ? nil : tail
    }

    /// A port nothing is listening on, from the kernel.
    static func freePort() -> Int? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, length) }
        }
        guard bound == 0 else { return nil }
        let named = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &length) }
        }
        guard named == 0 else { return nil }
        return Int(UInt16(bigEndian: address.sin_port))
    }

    /// 43 URL-safe characters: the backend accepts a supplied token of 32+.
    static func makeToken() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
