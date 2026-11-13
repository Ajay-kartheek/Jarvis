// The dashboard window
//
// An ordinary window on the page the daemon serves at /?view=dashboard: what
// Jarvis knows, what it made and what it is doing. Unlike the panel it has a
// real title bar, does not float, and exists only while it is open.

import AppKit
import WebKit

/// Only here so the test copy can say when the window itself is gone.
private final class DashboardWindow: NSWindow {
  deinit {
    if TestMode.on { Log.write("dashboard NSWindow deallocated") }
  }
}

final class DashboardController: NSObject, NSWindowDelegate {
  static let defaultSize = NSSize(width: 1180, height: 780)
  static let minimumSize = NSSize(width: 900, height: 600)
  static let frameName = "JarvisDashboard\(TestMode.nameSuffix)"
  // The page's own background, so nothing flashes while it loads.
  private static let background = NSColor(
    srgbRed: 5 / 255, green: 8 / 255, blue: 13 / 255, alpha: 1)

  /// What the page asked the app to do (see PageView.onAction).
  var onAction: ((_ action: String, _ message: [String: Any]) -> Void)?
  /// The user closed the window.
  var onClose: (() -> Void)?

  private let window: DashboardWindow
  private let page: PageView
  private let placeholder = NSTextField(labelWithString: "Starting Jarvis…")

  init(port: Int, hideFromScreenShare: Bool) {
    window = DashboardWindow(
      contentRect: NSRect(origin: .zero, size: DashboardController.defaultSize),
      styleMask: [.titled, .closable, .resizable, .miniaturizable],
      backing: .buffered, defer: false)
    page = PageView(name: "dashboard", query: "view=dashboard", port: port)
    super.init()

    window.title = "Jarvis"
    window.isReleasedWhenClosed = false
    window.appearance = NSAppearance(named: .darkAqua)
    window.backgroundColor = DashboardController.background
    // The page is built for at least this much room, so it is the page that gets it
    // (a minimum for the whole window would count the title bar into it).
    window.contentMinSize = DashboardController.minimumSize
    window.sharingType = hideFromScreenShare ? .none : .readOnly
    window.delegate = self

    let container = NSView(frame: NSRect(origin: .zero, size: DashboardController.defaultSize))
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
    window.contentView = container

    // The title bar is a real one, above the page, so the page keeps no strip clear.
    page.mark(titleStrip: 0)
    page.onAction = { [weak self] action, message in self?.onAction?(action, message) }
    page.onLost = { [weak self] in self?.placeholder.isHidden = false }
    page.onLoaded = { [weak self] title in
      guard let self else { return }
      self.placeholder.isHidden = true
      Log.write("dashboard loaded: \"\(title)\" frame \(NSStringFromRect(self.window.frame))")
    }

    let restored = window.setFrameUsingName(DashboardController.frameName)
    if !restored {
      window.setContentSize(DashboardController.defaultSize)
      window.center()
    }
    let remembered = window.setFrameAutosaveName(DashboardController.frameName)
    if TestMode.on {
      Log.write(
        "dashboard window: frame \(restored ? "restored" : "centred"),"
          + " position \(remembered ? "remembered" : "NOT remembered")")
    }
    page.load()
  }

  deinit {
    if TestMode.on { Log.write("dashboard window released") }
  }

  var isKey: Bool { window.isKeyWindow }
  var windowNumber: Int { window.windowNumber }

  /// Whether this is the window an event was meant for.
  func owns(_ other: NSWindow?) -> Bool { other === window }

  func update(port: Int, hideFromScreenShare: Bool) {
    window.sharingType = hideFromScreenShare ? .none : .readOnly
    page.update(port: port)
  }

  func load() {
    page.load()
  }

  /// Brings the window forward, out of the Dock if it was minimised there.
  func show() {
    if window.isMiniaturized { window.deminiaturize(nil) }
    NSApp.activate(ignoringOtherApps: true)
    window.makeKeyAndOrderFront(nil)
  }

  func close() {
    window.performClose(nil)
  }

  func minimise() {
    window.performMiniaturize(nil)
  }

  func windowWillClose(_ notification: Notification) {
    page.close()
    // AppKit can keep a closed window in memory for a few seconds (it does so while the panel
    // is up to take over the keyboard). While it lives it holds the saved-position name, and a
    // dashboard opened meanwhile would not get it. Keep the position, give up the name.
    window.saveFrame(usingName: DashboardController.frameName)
    window.setFrameAutosaveName("")
    Log.write("dashboard closed")
    onClose?()
  }
}
