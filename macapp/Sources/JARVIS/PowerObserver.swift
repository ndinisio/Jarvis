import Foundation

/// Watches ProcessInfo's thermal state and Low Power Mode — the two signals
/// the Python backend has no way to read itself — and reports every change
/// (plus the state as of the moment observation starts) so the screen
/// watcher can throttle under real pressure. See backend/jarvis/core/power.py.
final class PowerObserver {
    var onChange: ((_ thermalState: String, _ lowPowerMode: Bool) -> Void)?

    private var observers: [NSObjectProtocol] = []

    var thermalState: String {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: return "nominal"
        case .fair: return "fair"
        case .serious: return "serious"
        case .critical: return "critical"
        @unknown default: return "nominal"
        }
    }

    var lowPowerMode: Bool {
        ProcessInfo.processInfo.isLowPowerModeEnabled
    }

    func start() {
        let center = NotificationCenter.default
        observers.append(center.addObserver(
            forName: ProcessInfo.thermalStateDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.report() })
        observers.append(center.addObserver(
            forName: .NSProcessInfoPowerStateDidChange, object: nil, queue: .main
        ) { [weak self] _ in self?.report() })
    }

    /// Send the current values regardless of whether anything just changed —
    /// used once at startup, since a Mac can already be under thermal
    /// pressure before JARVIS ever launches.
    func report() {
        onChange?(thermalState, lowPowerMode)
    }

    deinit {
        let center = NotificationCenter.default
        observers.forEach { center.removeObserver($0) }
    }
}
