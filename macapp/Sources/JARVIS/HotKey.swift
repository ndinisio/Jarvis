import Carbon.HIToolbox

/// A system-wide keyboard shortcut (default ⌥Space) that works whichever
/// app is in front. Carbon's hot-key API needs no Accessibility permission,
/// unlike watching every key press.
final class HotKey {
    private var reference: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private let action: () -> Void

    init?(keyCode: UInt32 = UInt32(kVK_Space), modifiers: UInt32 = UInt32(optionKey),
          action: @escaping () -> Void) {
        self.action = action
        var pressed = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                    eventKind: UInt32(kEventHotKeyPressed))
        let context = Unmanaged.passUnretained(self).toOpaque()
        let installed = InstallEventHandler(GetApplicationEventTarget(), { _, _, userData in
            guard let userData else { return OSStatus(eventNotHandledErr) }
            let hotKey = Unmanaged<HotKey>.fromOpaque(userData).takeUnretainedValue()
            DispatchQueue.main.async { hotKey.action() }
            return OSStatus(noErr)
        }, 1, &pressed, context, &handler)
        guard installed == OSStatus(noErr) else { return nil }
        let identifier = EventHotKeyID(signature: OSType(0x4A52_5653), id: 1)   // 'JRVS'
        let registered = RegisterEventHotKey(keyCode, modifiers, identifier,
                                             GetApplicationEventTarget(), 0, &reference)
        guard registered == OSStatus(noErr) else { return nil }   // deinit removes the handler
    }

    deinit {
        if let reference { UnregisterEventHotKey(reference) }
        if let handler { RemoveEventHandler(handler) }
    }
}
