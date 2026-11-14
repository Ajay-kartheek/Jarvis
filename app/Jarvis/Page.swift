// One of the daemon's pages in a web view
//
// The floating panel and the dashboard window both show a page the Python
// daemon serves on 127.0.0.1. This is what they share: loading the page and
// trying again until the daemon answers, sending links that lead elsewhere to
// the browser, and passing on what the page asks the app to do.

import AppKit
import WebKit

/// The web view of a page. With `dragsWindow` on (the panel in its small form, which has no title
/// strip) a press on the page that then moves drags the window; a press that does not move is
/// an ordinary click, passed to the page whole, so the page still gets it. `dragZone` does the
/// same for presses within that many points of the top only (the full panel's header).
final class PageWebView: WKWebView {
  var dragsWindow = false
  var dragZone: CGFloat = 0
  /// The mouse moving this far (in points) makes a press into a drag.
  static let dragThreshold: CGFloat = 4
  /// Test copy only: where a drag event is on the screen, when the mouse itself is not moving.
  static var testScreenLocation: ((NSEvent) -> NSPoint)?

  /// A click on the page counts when the window is not the one in front: the panel floats over
  /// other apps, and its first click must not be spent on making it the key window.
  override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

  override func mouseDown(with event: NSEvent) {
    guard let window, dragsWindow || inDragZone(event) else {
      super.mouseDown(with: event)
      return
    }
    // Where the mouse is on the screen. (Not the event's place in the window, which would shift
    // as the window moves under it.)
    func screen(_ event: NSEvent) -> NSPoint {
      PageWebView.testScreenLocation?(event) ?? NSEvent.mouseLocation
    }
    let start = screen(event)
    let origin = window.frame.origin
    var dragging = false
    var released: NSEvent?
    window.trackEvents(
      matching: [.leftMouseDragged, .leftMouseUp], timeout: NSEvent.foreverDuration,
      mode: .eventTracking
    ) { next, stop in
      guard let next else {
        stop.pointee = true
        return
      }
      if next.type == .leftMouseUp {
        released = next
        stop.pointee = true
        return
      }
      let here = screen(next)
      let moved = NSPoint(x: here.x - start.x, y: here.y - start.y)
      if !dragging && hypot(moved.x, moved.y) >= PageWebView.dragThreshold { dragging = true }
      if dragging { window.setFrameOrigin(NSPoint(x: origin.x + moved.x, y: origin.y + moved.y)) }
    }
    if !dragging {
      super.mouseDown(with: event)
      if let released { super.mouseUp(with: released) }
    }
  }

  private func inDragZone(_ event: NSEvent) -> Bool {
    guard dragZone > 0 else { return false }
    let point = convert(event.locationInWindow, from: nil)
    return (isFlipped ? point.y : bounds.height - point.y) < dragZone
  }
}

final class PageView: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
  private static let bridgeName = "jarvis"
  // Files that run when opened are shown in the Finder instead.
  private static let runnableExtensions: Set<String> = [
    "app", "command", "sh", "zsh", "bash", "tool", "pkg", "mpkg", "dmg", "scpt", "applescript",
    "workflow", "action", "terminal", "jar", "py", "rb", "pl",
  ]

  let webView: PageWebView
  /// What the page asked for: hide, show, quit, dashboard, voice_start, voice_stop, size (with
  /// its message, which carries the mode).
  var onAction: ((_ action: String, _ message: [String: Any]) -> Void)?
  /// The page is on screen; the argument is its title.
  var onLoaded: ((String) -> Void)?
  /// The page went away and is being fetched again.
  var onLost: (() -> Void)?
  private(set) var loaded = false

  private let name: String  // for the log: "panel" or "dashboard"
  private let query: String
  private var port: Int
  private var retry: DispatchWorkItem?

  init(name: String, query: String, port: Int) {
    self.name = name
    self.query = query
    self.port = port
    let configuration = WKWebViewConfiguration()
    configuration.applicationNameForUserAgent = "Jarvis"
    // Jarvis speaks through the page, and nobody clicks before it does.
    configuration.mediaTypesRequiringUserActionForPlayback = []
    webView = PageWebView(frame: .zero, configuration: configuration)
    super.init()
    // Whatever is behind the web view shows until the page has drawn itself.
    webView.setValue(false, forKey: "drawsBackground")
    webView.underPageBackgroundColor = .clear
    webView.isInspectable = true
    webView.navigationDelegate = self
    webView.uiDelegate = self
    webView.configuration.userContentController.add(self, name: PageView.bridgeName)
    if TestMode.on {
      let needs = webView.configuration.mediaTypesRequiringUserActionForPlayback
      let answer = needs.isEmpty ? "no" : "yes (\(needs.rawValue))"
      Log.write("\(name): sound needs a user action to play: \(answer)")
    }
  }

  deinit {
    if TestMode.on { Log.write("\(name) page released") }
  }

  /// Tells the page it is inside the app, how tall the strip at the top is that belongs to the
  /// window rather than to the page, and whether the panel is in its small form. It is told at
  /// once, and again whenever the page is loaded afresh.
  func mark(titleStrip: Int, mini: Bool = false) {
    let source =
      "document.documentElement.classList.add('jarvis-native');"
      + "document.documentElement.classList.\(mini ? "add" : "remove")('jarvis-mini');"
      + "document.documentElement.style.setProperty('--jarvis-titlebar', '\(titleStrip)px');"
    let scripts = webView.configuration.userContentController
    scripts.removeAllUserScripts()  // this is the only one: the new one replaces the old
    scripts.addUserScript(
      WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true))
    run(source)
  }

  /// Lets go of the page for good. The web view holds on to this object until then.
  func close() {
    retry?.cancel()
    webView.stopLoading()
    webView.configuration.userContentController.removeScriptMessageHandler(
      forName: PageView.bridgeName)
    webView.navigationDelegate = nil
    webView.uiDelegate = nil
  }

  // MARK: Loading the page

  private var pageURL: URL { URL(string: "http://127.0.0.1:\(port)/?\(query)")! }

  func update(port: Int) {
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

  /// Runs a line of script in the page; nothing comes back.
  func run(_ script: String) {
    webView.evaluateJavaScript("\(script); null") { _, _ in }
  }

  /// Runs an expression in the page and says whether it came out true (false when it could not run).
  func ask(_ script: String, _ done: @escaping (Bool) -> Void) {
    webView.evaluateJavaScript(script) { result, _ in done((result as? Bool) ?? false) }
  }

  /// The daemon is not answering yet (or went away): try again in a second.
  private func loadAgainSoon(_ why: String) {
    if loaded { Log.write("\(name) lost its page (\(why))") }
    loaded = false
    onLost?()
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
    webView.evaluateJavaScript("document.title") { [weak self] title, _ in
      self?.onLoaded?(title as? String ?? "")
    }
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
    // Anything else leaves the app, and only when the user clicked it.
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
    if TestMode.flag("JARVIS_TEST_NO_OPEN") {
      Log.write("\(name): a link leaves the app for the browser: \(url) (not opened)")
      return
    }
    switch url.scheme {
    case "http", "https", "mailto":
      NSWorkspace.shared.open(url)
    case "file":
      var executable = false
      if let values = try? url.resourceValues(forKeys: [.isExecutableKey, .isDirectoryKey]) {
        executable = values.isExecutable == true && values.isDirectory != true
      }
      if executable || PageView.runnableExtensions.contains(url.pathExtension.lowercased()) {
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
    if TestMode.on { Log.write("\(name): the page sent \"\(action)\"") }
    onAction?(action, body)
  }
}
