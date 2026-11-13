// The floating panel
//
// One window that stays above everything, full-screen calls included, and
// shows the orb: the page the Python daemon serves at /?panel=1.

import AppKit
import WebKit

/// A panel that can take the keyboard even though it has no ordinary title bar.
final class JarvisPanel: NSPanel {
  override var canBecomeKey: Bool { true }
  override var canBecomeMain: Bool { false }
}

final class PanelController: NSObject {
  static let defaultSize = NSSize(width: 460, height: 760)
  static let minimumSize = NSSize(width: 380, height: 480)
  static let edgeMargin: CGFloat = 16
  static let cornerRadius: CGFloat = 14
  static let frameName = "JarvisPanel\(TestMode.nameSuffix)"
  private static let escapeKey: UInt16 = 53

  /// What the page asked the app to do (see PageView.onAction).
  var onAction: ((String) -> Void)?

  private let panel: JarvisPanel
  private let page: PageView
  private let placeholder = NSTextField(labelWithString: "Starting Jarvis…")
  private var pendingReason: String?
  private var previousApp: NSRunningApplication?
  private var keyMonitor: Any?

  var isVisible: Bool { panel.isVisible }
  var isKey: Bool { panel.isKeyWindow }

  init(port: Int, hideFromScreenShare: Bool) {
    panel = JarvisPanel(
      contentRect: PanelController.defaultFrame(),
      styleMask: [.nonactivatingPanel, .titled, .fullSizeContentView, .resizable],
      backing: .buffered, defer: false)
    page = PageView(name: "panel", query: "panel=1", port: port)
    super.init()

    panel.title = "Jarvis"
    panel.titleVisibility = .hidden
    panel.titlebarAppearsTransparent = true
    for button in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
      panel.standardWindowButton(button)?.isHidden = true
    }
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

    // Dark glass behind the page, so the panel is never an empty hole while
    // the daemon is still starting.
    let container = NSView(frame: NSRect(origin: .zero, size: PanelController.defaultSize))
    container.wantsLayer = true
    container.layer?.cornerRadius = PanelController.cornerRadius
    container.layer?.masksToBounds = true
    let glass = NSVisualEffectView(frame: container.bounds)
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
    page.onAction = { [weak self] action in self?.onAction?(action) }
    page.onLost = { [weak self] in self?.placeholder.isHidden = false }
    page.onLoaded = { [weak self] title in
      guard let self else { return }
      self.placeholder.isHidden = true
      Log.write(
        "panel loaded: \"\(title)\" frame \(self.frameDescription)"
          + " title strip \(self.titleStripHeight) px")
      if let reason = self.pendingReason, self.panel.isVisible { self.tellPage(reason) }
      self.pendingReason = nil
    }

    if !panel.setFrameUsingName(PanelController.frameName) {
      panel.setFrame(PanelController.defaultFrame(), display: false)
    }
    panel.setFrameAutosaveName(PanelController.frameName)

    // Esc hides the panel wherever the keyboard focus is inside it.
    keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
      guard let self, event.keyCode == PanelController.escapeKey, event.window === self.panel
      else { return event }
      self.hide()
      return nil
    }
  }

  /// Top right of the main screen, a margin in from the edges.
  private static func defaultFrame() -> NSRect {
    guard let screen = NSScreen.main ?? NSScreen.screens.first else {
      return NSRect(origin: NSPoint(x: 200, y: 200), size: defaultSize)
    }
    let area = screen.visibleFrame
    let height = min(defaultSize.height, area.height - 2 * edgeMargin)
    return NSRect(
      x: area.maxX - defaultSize.width - edgeMargin, y: area.maxY - height - edgeMargin,
      width: defaultSize.width, height: height)
  }

  var frameDescription: String { NSStringFromRect(panel.frame) }

  private var titleStripHeight: Int {
    Int((panel.frame.height - panel.contentLayoutRect.height).rounded())
  }

  /// A saved position can point at a display that is no longer connected.
  private func bringOntoScreen() {
    let visible = NSScreen.screens.contains { screen in
      let shared = screen.visibleFrame.intersection(panel.frame)
      return shared.width >= 120 && shared.height >= 120
    }
    if !visible { panel.setFrame(PanelController.defaultFrame(), display: false) }
  }

  // MARK: The page

  func update(port: Int, hideFromScreenShare: Bool) {
    panel.sharingType = hideFromScreenShare ? .none : .readOnly
    page.update(port: port)
  }

  func load() {
    page.load()
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
}
