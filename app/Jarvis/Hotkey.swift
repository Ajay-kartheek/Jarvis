// A system-wide keyboard shortcut
//
// Carbon's hot keys are the one way to get a global shortcut without asking
// for the Accessibility permission.

import Carbon.HIToolbox
import Foundation

final class Hotkey {
  private let keyCode: UInt32
  private let modifiers: UInt32
  private let action: () -> Void
  private var hotKey: EventHotKeyRef?
  private var handler: EventHandlerRef?

  /// Control+Option+J.
  static func controlOptionJ(action: @escaping () -> Void) -> Hotkey {
    Hotkey(keyCode: UInt32(kVK_ANSI_J), modifiers: UInt32(controlKey | optionKey), action: action)
  }

  init(keyCode: UInt32, modifiers: UInt32, action: @escaping () -> Void) {
    self.keyCode = keyCode
    self.modifiers = modifiers
    self.action = action
  }

  /// Returns noErr when the shortcut is now ours. Another app holding it is the usual failure.
  func register() -> OSStatus {
    var pressed = EventTypeSpec(
      eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
    let installed = InstallEventHandler(
      GetApplicationEventTarget(),
      { _, _, context in
        guard let context else { return OSStatus(eventNotHandledErr) }
        let hotkey = Unmanaged<Hotkey>.fromOpaque(context).takeUnretainedValue()
        DispatchQueue.main.async { hotkey.action() }
        return noErr
      },
      1, &pressed, Unmanaged.passUnretained(self).toOpaque(), &handler)
    guard installed == noErr else { return installed }
    let identifier = EventHotKeyID(signature: OSType(0x4A52_5653), id: 1)  // 'JRVS'
    return RegisterEventHotKey(
      keyCode, modifiers, identifier, GetApplicationEventTarget(), 0, &hotKey)
  }
}
