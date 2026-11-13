// The floating panel
//
// One window that stays above everything, full-screen calls included, and
// shows the interface the Python daemon serves on 127.0.0.1.

import AppKit
import WebKit

/// A panel that can take the keyboard even though it has no ordinary title bar.
final class JarvisPanel: NSPanel {
  override var canBecomeKey: Bool { true }
  override var canBecomeMain: Bool { false }
}

final class PanelController: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
  static let defaultSize = NSSize(width: 460, height: 760)
  static let minimumSize = NSSize(width: 380, height: 480)
  static let edgeMargin: CGFloat = 16
  static let cornerRadius: CGFloat = 14
  static let frameName = "JarvisPanel"
  private static let escapeKey: UInt16 = 53
  // Files that run when opened are shown in the Finder instead.
  private static let runnableExtensions: Set<String> = [
    "app", "command", "sh", "zsh", "bash", "tool", "pkg", "mpkg", "dmg", "scpt", "applescript",
    "workflow", "action", "terminal", "jar", "py", "rb", "pl",
  ]

  var onQuit: (() -> Void)?

  private let panel: JarvisPanel
  private let webView: WKWebView
  private let placeholder = NSTextField(labelWithString: "Starting Jarvis…")
  private var port: Int
  private var loaded = false
  private var pendingReason: String?
  private var retry: DispatchWorkItem?
  private var previousApp: NSRunningApplication?
  private var keyMonitor: Any?

  var isVisible: Bool { panel.isVisible }
  var isKey: Bool { panel.isKeyWindow }

  init(port: Int, hideFromScreenShare: Bool) {
    self.port = port
    panel = JarvisPanel(
      contentRect: PanelController.defaultFrame(),
      styleMask: [.nonactivatingPanel, .titled, .fullSizeContentView, .resizable],
      backing: .buffered, defer: false)
    let configuration = WKWebViewConfiguration()
    configuration.applicationNameForUserAgent = "Jarvis"
    webView = WKWebView(frame: .zero, configuration: configuration)
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

    webView.frame = container.bounds
    webView.autoresizingMask = [.width, .height]
    webView.setValue(false, forKey: "drawsBackground")
    webView.underPageBackgroundColor = .clear
    webView.isInspectable = true
    webView.navigationDelegate = self
    webView.uiDelegate = self
    webView.configuration.userContentController.add(self, name: "jarvis")
    container.addSubview(webView)
    panel.contentView = container

    // The strip at the top belongs to the window (it is where the panel is
    // dragged), so the page is told how tall it is and keeps clear of it.
    let marker = WKUserScript(
      source: "document.documentElement.classList.add('jarvis-native');"
        + "document.documentElement.style.setProperty('--jarvis-titlebar', '\(titleStripHeight)px');",
      injectionTime: .atDocumentStart, forMainFrameOnly: true)
    webView.configuration.userContentController.addUserScript(marker)

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

  // MARK: Loading the page

  private var pageURL: URL { URL(string: "http://127.0.0.1:\(port)/?panel=1")! }

  func update(port: Int, hideFromScreenShare: Bool) {
    panel.sharingType = hideFromScreenShare ? .none : .readOnly
    guard port != self.port else { return }
    self.port = port
    load()
  }

  func load() {
    retry?.cancel()
    var request = URLRequest(url: pageURL)
    request.cachePolicy = .reloadIgnoringLocalCacheData
    request.timeoutInterval = 5
    webView.load(request)
  }

  /// The daemon is not answering yet (or went away): try again in a second.
  private func loadAgainSoon(_ why: String) {
    if loaded { Log.write("panel lost its page (\(why))") }
    loaded = false
    placeholder.isHidden = false
    retry?.cancel()
    let work = DispatchWorkItem { [weak self] in self?.load() }
    retry = work
    DispatchQueue.main.asyncAfter(deadline: .now() + 1, execute: work)
  }

  func webView(
    _ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
    decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
  ) {
    if navigationResponse.isForMainFrame,
      let response = navigationResponse.response as? HTTPURLResponse, response.statusCode != 200
    {
      decisionHandler(.cancel)
      loadAgainSoon("HTTP \(response.statusCode)")
      return
    }
    decisionHandler(.allow)
  }

  func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
    loaded = true
    placeholder.isHidden = true
    webView.evaluateJavaScript("document.title") { [weak self] title, _ in
      guard let self else { return }
      Log.write(
        "panel loaded: \"\(title as? String ?? "")\" frame \(self.frameDescription)"
          + " title strip \(self.titleStripHeight) px")
    }
    if let reason = pendingReason, panel.isVisible { tellPage(reason) }
    pendingReason = nil
  }

  func webView(
    _ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
    withError error: Error
  ) {
    loadAgainSoon(error.localizedDescription)
  }

  func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
    guard (error as NSError).code != NSURLErrorCancelled else { return }
    loadAgainSoon(error.localizedDescription)
  }

  func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
    loadAgainSoon("the page's process ended")
  }

  // MARK: Links

  private func isOurPage(_ url: URL) -> Bool {
    url.scheme == "http" && (url.host == "127.0.0.1" || url.host == "localhost")
      && (url.port ?? 80) == port
  }

  func webView(
    _ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
    decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
  ) {
    guard let url = navigationAction.request.url else {
      decisionHandler(.cancel)
      return
    }
    if isOurPage(url) || url.scheme == "about" {
      decisionHandler(.allow)
      return
    }
    // Anything else leaves the panel, and only when the user clicked it.
    if navigationAction.navigationType == .linkActivated { openOutside(url) }
    decisionHandler(.cancel)
  }

  /// Links that ask for a new window (target="_blank").
  func webView(
    _ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
    for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures
  ) -> WKWebView? {
    if navigationAction.navigationType == .linkActivated, let url = navigationAction.request.url {
      if isOurPage(url) { webView.load(navigationAction.request) } else { openOutside(url) }
    }
    return nil
  }

  private func openOutside(_ url: URL) {
    switch url.scheme {
    case "http", "https", "mailto":
      NSWorkspace.shared.open(url)
    case "file":
      var executable = false
      if let values = try? url.resourceValues(forKeys: [.isExecutableKey, .isDirectoryKey]) {
        executable = values.isExecutable == true && values.isDirectory != true
      }
      if executable || PanelController.runnableExtensions.contains(url.pathExtension.lowercased()) {
        NSWorkspace.shared.activateFileViewerSelecting([url])
      } else {
        NSWorkspace.shared.open(url)
      }
    default:
      Log.write("ignored a link with scheme \(url.scheme ?? "none")")
    }
  }

  // MARK: Messages from the page

  func userContentController(
    _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
  ) {
    let origin = message.frameInfo.securityOrigin
    guard message.frameInfo.isMainFrame, origin.host == "127.0.0.1" || origin.host == "localhost",
      origin.port == port, let body = message.body as? [String: Any],
      let action = body["action"] as? String
    else { return }
    switch action {
    case "hide": hide()
    case "show": show(reason: "attention", activate: false)
    case "quit": onQuit?()
    default: Log.write("the page sent an unknown action: \(action)")
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
      panel.makeFirstResponder(webView)
    } else {
      panel.orderFrontRegardless()
    }
    Log.write("panel shown (\(reason))")
    if loaded { tellPage(reason) } else { pendingReason = reason }
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
    webView.evaluateJavaScript(
      "window.jarvisNative && window.jarvisNative.onShow && window.jarvisNative.onShow('\(reason)'); null"
    ) { _, _ in }
  }
}
