// The floating panel
//
// One window that stays above everything, full-screen calls included, and
// shows the orb: the page the Python daemon serves at /?panel=1. It has two forms: the
// full panel, and a small floating square (the orb alone) that the page asks for with the
// bridge action {action: "size", mode: "mini" | "full"}.

import AppKit
import WebKit

/// A panel that can take the keyboard even though it has no ordinary title bar.
final class JarvisPanel: NSPanel {
  override var canBecomeKey: Bool { true }
  override var canBecomeMain: Bool { false }
}

final class PanelController: NSObject {
  enum Size: String {
    case full
    case mini
  }

  static let defaultSize = NSSize(width: 460, height: 760)
  static let minimumSize = NSSize(width: 380, height: 480)
  static let miniSize = NSSize(width: 150, height: 150)
  static let edgeMargin: CGFloat = 16
  static let miniMargin: CGFloat = 24
  static let cornerRadius: CGFloat = 14
  /// How long the panel takes to change from one form to the other.
  static let resizeSeconds: TimeInterval = 0.25
  // Each form keeps its own place and size. (Apart from the test copy's suffix, these are the
  // autosave names JarvisPanel and JarvisPanelMini.)
  static let fullFrameName = "JarvisPanel\(TestMode.nameSuffix)"
  static let miniFrameName = "JarvisPanelMini\(TestMode.nameSuffix)"
  private static let escapeKey: UInt16 = 53
  // The full panel has a title strip that belongs to the window (it is where the panel is
  // dragged); the small one has none, and moves by its whole background.
  private static let fullStyle: NSWindow.StyleMask = [
    .nonactivatingPanel, .titled, .fullSizeContentView, .resizable,
  ]
  private static let miniStyle: NSWindow.StyleMask = [.nonactivatingPanel]
  /// Below the title strip, the page's header (the name, the status and the buttons) moves the
  /// full panel too: this tall.
  static let headerHeight: CGFloat = 48

  /// What the page asked the app to do (see PageView.onAction).
  var onAction: ((_ action: String, _ message: [String: Any]) -> Void)?

  private let panel: JarvisPanel
  private let page: PageView
  private let placeholder = NSTextField(labelWithString: "Starting Jarvis…")
  // Dark glass behind the full panel. The small orb has none: it floats on the desktop by itself.
  private let glass = NSVisualEffectView()
  private var pendingReason: String?
  private var previousApp: NSRunningApplication?
  private var keyMonitor: Any?
  // Counts the changes of form, so that one cut short by the next does not finish as if it had run.
  private var changes = 0

  /// The form the panel is in. It starts full, and keeps its form while hidden and shown.
  private(set) var size = Size.full

  var isVisible: Bool { panel.isVisible }
  var isKey: Bool { panel.isKeyWindow }
  var frame: NSRect { panel.frame }
  var windowNumber: Int { panel.windowNumber }

  init(port: Int, hideFromScreenShare: Bool) {
    panel = JarvisPanel(
      contentRect: PanelController.defaultFrame(for: .full),
      styleMask: PanelController.fullStyle, backing: .buffered, defer: false)
    page = PageView(name: "panel", query: "panel=1", port: port)
    super.init()

    panel.title = "Jarvis"
    panel.level = .floating
    panel.isFloatingPanel = true
    panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
    panel.hidesOnDeactivate = false
    panel.isReleasedWhenClosed = false
    panel.isOpaque = false
    panel.backgroundColor = .clear
    panel.hasShadow = true
    panel.appearance = NSAppearance(named: .darkAqua)
    panel.animationBehavior = .utilityWindow
    panel.minSize = PanelController.minimumSize
    panel.sharingType = hideFromScreenShare ? .none : .readOnly
    hideTitleBar()

    // Dark glass behind the page, so the panel is never an empty hole while
    // the daemon is still starting.
    let container = NSView(frame: NSRect(origin: .zero, size: PanelController.defaultSize))
    container.wantsLayer = true
    container.layer?.cornerRadius = PanelController.cornerRadius
    container.layer?.masksToBounds = true
    glass.frame = container.bounds
    glass.material = .hudWindow
    glass.blendingMode = .behindWindow
    glass.state = .active
    glass.autoresizingMask = [.width, .height]
    container.addSubview(glass)

    placeholder.textColor = .secondaryLabelColor
    placeholder.font = .systemFont(ofSize: 13)
    placeholder.sizeToFit()
    placeholder.setFrameOrigin(
      NSPoint(
        x: (container.bounds.width - placeholder.frame.width) / 2,
        y: (container.bounds.height - placeholder.frame.height) / 2))
    placeholder.autoresizingMask = [.minXMargin, .maxXMargin, .minYMargin, .maxYMargin]
    container.addSubview(placeholder)

    page.webView.frame = container.bounds
    page.webView.autoresizingMask = [.width, .height]
    container.addSubview(page.webView)
    panel.contentView = container

    // The strip at the top belongs to the window (it is where the panel is
    // dragged), so the page is told how tall it is and keeps clear of it.
    page.mark(titleStrip: titleStripHeight)
    page.webView.dragZone = dragZoneHeight
    page.onAction = { [weak self] action, message in self?.onAction?(action, message) }
    page.onLost = { [weak self] in self?.placeholder.isHidden = false }
    page.onLoaded = { [weak self] title in
      guard let self else { return }
      self.placeholder.isHidden = true
      Log.write(
        "panel loaded: \"\(title)\" frame \(self.frameDescription)"
          + " title strip \(self.titleStripHeight) px, form \(self.size.rawValue)")
      if let reason = self.pendingReason, self.panel.isVisible { self.tellPage(reason) }
      self.pendingReason = nil
    }

    if !panel.setFrameUsingName(PanelController.fullFrameName) {
      panel.setFrame(PanelController.defaultFrame(for: .full), display: false)
    }
    panel.setFrameAutosaveName(PanelController.fullFrameName)

    if TestMode.on {  // where the panel goes, to see what moves it
      for name in [NSWindow.didMoveNotification, NSWindow.didResizeNotification] {
        NotificationCenter.default.addObserver(forName: name, object: panel, queue: .main) {
          [weak self] note in
          guard let self else { return }
          Log.write("test: panel \(note.name.rawValue) -> \(self.frameDescription)")
        }
      }
    }

    // Esc, wherever the keyboard focus is inside the panel, stops Jarvis talking; when he is not
    // talking, it hides the panel.
    keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
      guard let self, event.keyCode == PanelController.escapeKey, event.window === self.panel
      else { return event }
      self.interrupt { stopped in
        if !stopped { self.hide() }
      }
      return nil
    }
  }

  /// Stops Jarvis talking, if he is, and says whether he was. The page knows: it plays the voice.
  /// (A page that is not there says no.)
  func interrupt(_ done: @escaping (Bool) -> Void) {
    page.ask("window.jarvisInterrupt ? window.jarvisInterrupt() === true : false", done)
  }

  /// The full panel has no visible title bar: the page runs up under it.
  private func hideTitleBar() {
    panel.titleVisibility = .hidden
    panel.titlebarAppearsTransparent = true
    for button in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
      panel.standardWindowButton(button)?.isHidden = true
    }
  }

  /// Top right of the main screen, a margin in from the edges; the small orb bottom right.
  private static func defaultFrame(for size: Size) -> NSRect {
    let full = size == .full
    let wanted = full ? defaultSize : miniSize
    guard let screen = NSScreen.main ?? NSScreen.screens.first else {
      return NSRect(origin: NSPoint(x: 200, y: 200), size: wanted)
    }
    let area = screen.visibleFrame
    if !full {
      return NSRect(
        x: area.maxX - wanted.width - miniMargin, y: area.minY + miniMargin,
        width: wanted.width, height: wanted.height)
    }
    let height = min(wanted.height, area.height - 2 * edgeMargin)
    return NSRect(
      x: area.maxX - wanted.width - edgeMargin, y: area.maxY - height - edgeMargin,
      width: wanted.width, height: height)
  }

  var frameDescription: String { NSStringFromRect(panel.frame) }

  private var titleStripHeight: Int {
    Int((panel.frame.height - panel.contentLayoutRect.height).rounded())
  }

  /// How far down from the top a press that moves drags the full panel: the strip and the header.
  private var dragZoneHeight: CGFloat {
    size == .full ? CGFloat(titleStripHeight) + PanelController.headerHeight : 0
  }

  private var currentFrameName: String {
    size == .full ? PanelController.fullFrameName : PanelController.miniFrameName
  }

  /// The frame AppKit saved under a name ("x y width height" and then the screen's, in the user
  /// defaults), without putting it on the window.
  private static func savedFrame(named name: String) -> NSRect? {
    guard let text = UserDefaults.standard.string(forKey: "NSWindow Frame \(name)") else {
      return nil
    }
    let numbers = text.split(separator: " ").compactMap { Double($0) }
    guard numbers.count >= 4, numbers[2] > 0, numbers[3] > 0 else { return nil }
    return NSRect(x: numbers[0], y: numbers[1], width: numbers[2], height: numbers[3])
  }

  /// Enough of a frame on some screen to be found and used.
  private static func isOnScreen(_ frame: NSRect) -> Bool {
    NSScreen.screens.contains { screen in
      let shared = screen.visibleFrame.intersection(frame)
      return shared.width >= min(120, frame.width / 2) && shared.height >= min(120, frame.height / 2)
    }
  }

  /// A saved position can point at a display that is no longer connected.
  private func bringOntoScreen() {
    if !PanelController.isOnScreen(panel.frame) {
      panel.setFrame(PanelController.defaultFrame(for: size), display: false)
    }
  }

  // MARK: The page

  func update(port: Int, hideFromScreenShare: Bool) {
    panel.sharingType = hideFromScreenShare ? .none : .readOnly
    page.update(port: port)
  }

  func load() {
    page.load()
  }

  // MARK: The two forms

  /// Turns the panel into the small orb, or back into the full panel. Each form is put where it
  /// was last left; the change takes a quarter of a second. The page is told by the class
  /// "jarvis-mini" on its html element and by --jarvis-titlebar (0px without a title strip).
  func setSize(_ wanted: Size) {
    guard wanted != size else { return }
    let began = DispatchTime.now()
    let animated = panel.isVisible
    changes += 1
    let change = changes
    // The place the panel is leaving belongs to its old form. Autosaving rests while the frame
    // changes, so that no frame on the way is kept under either name.
    if panel.frameAutosaveName == currentFrameName { panel.saveFrame(usingName: currentFrameName) }
    panel.setFrameAutosaveName("")
    size = wanted
    panel.styleMask = wanted == .mini ? PanelController.miniStyle : PanelController.fullStyle
    if wanted == .full { hideTitleBar() }
    glass.isHidden = wanted == .mini
    panel.hasShadow = wanted == .full
    // Any size may be passed through on the way; the limits of the new form apply once there.
    panel.minSize = PanelController.miniSize
    panel.maxSize = NSSize(width: 100_000, height: 100_000)
    panel.invalidateShadow()
    // Not while the frame is on its way: a drag and the animation would pull against each other.
    page.webView.dragsWindow = false
    page.webView.dragZone = 0
    page.mark(titleStrip: titleStripHeight, mini: wanted == .mini)

    var target =
      PanelController.savedFrame(named: currentFrameName)
      ?? PanelController.defaultFrame(for: wanted)
    if wanted == .mini { target.size = PanelController.miniSize }
    if !PanelController.isOnScreen(target) { target = PanelController.defaultFrame(for: wanted) }

    let finish = { [weak self] in
      guard let self, self.changes == change else { return }
      if wanted == .mini {
        self.panel.minSize = PanelController.miniSize
        self.panel.maxSize = PanelController.miniSize
      } else {
        self.panel.minSize = PanelController.minimumSize
      }
      self.page.webView.dragsWindow = wanted == .mini
      self.page.webView.dragZone = self.dragZoneHeight
      // From here on AppKit keeps the new form's frame again (and saves this one now).
      self.panel.setFrameAutosaveName(self.currentFrameName)
      let took = Int((DispatchTime.now().uptimeNanoseconds &- began.uptimeNanoseconds) / 1_000_000)
      Log.write(
        "panel: now \(wanted.rawValue), frame \(self.frameDescription),"
          + " title strip \(self.titleStripHeight) px, took \(took) ms"
          + (animated ? "" : " (hidden: no animation)"))
    }
    if animated {
      NSAnimationContext.runAnimationGroup(
        { context in
          context.duration = PanelController.resizeSeconds
          context.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
          panel.animator().setFrame(target, display: true)
        }, completionHandler: finish)
    } else {
      panel.setFrame(target, display: true)
      finish()
    }
  }

  // MARK: Showing and hiding

  /// `activate` also gives the panel the keyboard; without it the app in front keeps it.
  func show(reason: String, activate: Bool) {
    if !panel.isVisible { bringOntoScreen() }
    if activate {
      if !NSApp.isActive {
        let front = NSWorkspace.shared.frontmostApplication
        previousApp = front?.processIdentifier == getpid() ? nil : front
      }
      NSApp.activate(ignoringOtherApps: true)
      panel.makeKeyAndOrderFront(nil)
      panel.makeFirstResponder(page.webView)
    } else {
      panel.orderFrontRegardless()
    }
    Log.write("panel shown (\(reason))")
    if page.loaded { tellPage(reason) } else { pendingReason = reason }
  }

  func hide() {
    guard panel.isVisible else { return }
    let wasActive = NSApp.isActive
    panel.orderOut(nil)
    Log.write("panel hidden")
    // Hand the keyboard back to the app the user was in.
    if wasActive, let previous = previousApp, !previous.isTerminated {
      NSApp.yieldActivation(to: previous)
      previous.activate(options: [])
    }
    previousApp = nil
  }

  private func tellPage(_ reason: String) {
    page.run(
      "window.jarvisNative && window.jarvisNative.onShow && window.jarvisNative.onShow('\(reason)')")
  }

  // MARK: For the test copy

  /// Moves (and, in the full form, resizes) the panel as the user would with the mouse.
  func testChangeFrame(dx: CGFloat, dy: CGFloat, dwidth: CGFloat = 0, dheight: CGFloat = 0) {
    var frame = panel.frame
    frame.origin.x += dx
    frame.origin.y += dy
    frame.size.width += dwidth
    frame.size.height += dheight
    panel.setFrame(frame, display: true)
  }

  /// Presses the mouse on the page at a point (from the panel's top left, in points), moves it
  /// by the given steps, and lets go, with real events through the window's own dispatch.
  func testPress(at point: NSPoint, steps: [NSPoint]) {
    let origin = panel.frame.origin
    let top = panel.frame.height
    let window = panel.windowNumber
    func event(_ type: NSEvent.EventType, _ offset: NSPoint, _ number: Int) -> NSEvent? {
      NSEvent.mouseEvent(
        with: type, location: NSPoint(x: point.x + offset.x, y: top - point.y - offset.y),
        modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window,
        context: nil, eventNumber: number, clickCount: 1, pressure: type == .leftMouseUp ? 0 : 1)
    }
    PageWebView.testScreenLocation = { moved in
      // The screen position of a drag event, taken from where the panel was at the start.
      NSPoint(x: origin.x + moved.locationInWindow.x, y: origin.y + moved.locationInWindow.y)
    }
    defer { PageWebView.testScreenLocation = nil }
    var number = 1
    for step in steps {
      number += 1
      if let drag = event(.leftMouseDragged, step, number) { NSApp.postEvent(drag, atStart: false) }
    }
    if let up = event(.leftMouseUp, steps.last ?? .zero, number + 1) {
      NSApp.postEvent(up, atStart: false)
    }
    if let down = event(.leftMouseDown, .zero, 1) { NSApp.sendEvent(down) }
  }
}
