// A system-wide keyboard shortcut
//
// Carbon's hot keys are the one way to get a global shortcut without asking
// for the Accessibility permission. They report the key going down and coming
// up, which is what lets one shortcut be both a tap and a hold.

import Carbon.HIToolbox
import Foundation

final class Hotkey {
  private let keyCode: UInt32
  private let modifiers: UInt32
  private let onDown: () -> Void
  private let onUp: () -> Void
  private var hotKey: EventHotKeyRef?
  private var handler: EventHandlerRef?

  /// Control+Option+J; the test copy takes Control+Option+K so the two do not collide.
  static var letter: String { TestMode.on ? "K" : "J" }

  static func controlOption(onDown: @escaping () -> Void, onUp: @escaping () -> Void) -> Hotkey {
    Hotkey(
      keyCode: UInt32(TestMode.on ? kVK_ANSI_K : kVK_ANSI_J),
      modifiers: UInt32(controlKey | optionKey), onDown: onDown, onUp: onUp)
  }

  init(
    keyCode: UInt32, modifiers: UInt32, onDown: @escaping () -> Void, onUp: @escaping () -> Void
  ) {
    self.keyCode = keyCode
    self.modifiers = modifiers
    self.onDown = onDown
    self.onUp = onUp
  }

  /// Returns noErr when the shortcut is now ours. Another app holding it is the usual failure.
  func register() -> OSStatus {
    var kinds = [
      EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
      EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased)),
    ]
    let installed = InstallEventHandler(
      GetApplicationEventTarget(),
      { _, event, context in
        guard let event, let context else { return OSStatus(eventNotHandledErr) }
        let hotkey = Unmanaged<Hotkey>.fromOpaque(context).takeUnretainedValue()
        let released = GetEventKind(event) == UInt32(kEventHotKeyReleased)
        DispatchQueue.main.async { released ? hotkey.onUp() : hotkey.onDown() }
        return noErr
      },
      kinds.count, &kinds, Unmanaged.passUnretained(self).toOpaque(), &handler)
    guard installed == noErr else { return installed }
    let identifier = EventHotKeyID(signature: OSType(0x4A52_5653), id: 1)  // 'JRVS'
    return RegisterEventHotKey(
      keyCode, modifiers, identifier, GetApplicationEventTarget(), 0, &hotKey)
  }
}
