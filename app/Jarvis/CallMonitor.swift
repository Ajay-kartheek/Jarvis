// Noticing a call
//
// Core Audio knows which processes have the microphone or the speakers open.
// A call is on when one of the call apps (Teams, Zoom, a browser, ...) has the
// microphone open. Reading this needs no permission and captures no audio.

import AppKit
import CoreAudio
import Foundation

struct AudioProcess: Equatable {
  let pid: pid_t
  /// The bundle identifier Core Audio reports for the process itself, often a helper.
  let bundleID: String
  /// The bundle identifier of the app the process belongs to.
  let owner: String
  let input: Bool
  let output: Bool
}

// MARK: - Which app a process belongs to

/// Audio is usually handled by a helper process ("Google Chrome Helper", a web
/// view). This finds the app the user would name.
final class OwnerResolver {
  private typealias ResponsibleLookup = @convention(c) (pid_t) -> pid_t

  // The system's own record of which app answers for a process. Not a public
  // call, so it is looked up by name and skipped when missing.
  private static let responsibleLookup: ResponsibleLookup? = {
    let everywhere = UnsafeMutableRawPointer(bitPattern: -2)  // RTLD_DEFAULT
    guard let symbol = dlsym(everywhere, "responsibility_get_pid_responsible_for_pid") else {
      return nil
    }
    return unsafeBitCast(symbol, to: ResponsibleLookup.self)
  }()

  private var known: [pid_t: String] = [:]

  func owner(of pid: pid_t, fallback: String) -> String {
    if let found = known[pid] { return found }
    guard let found = resolve(pid) else { return fallback }
    known[pid] = found
    return found
  }

  /// Process numbers are reused, so forget the ones that are gone.
  func keep(only alive: Set<pid_t>) {
    known = known.filter { alive.contains($0.key) }
  }

  private func resolve(_ pid: pid_t) -> String? {
    if let lookup = OwnerResolver.responsibleLookup {
      let responsible = lookup(pid)
      if responsible > 0,
        let identifier = NSRunningApplication(processIdentifier: responsible)?.bundleIdentifier
      {
        return identifier
      }
    }
    // Otherwise walk up the parents to the first real app.
    var current = pid
    var firstWithIdentifier: String?
    for _ in 0..<12 {
      if let app = NSRunningApplication(processIdentifier: current),
        let identifier = app.bundleIdentifier
      {
        if app.activationPolicy != .prohibited { return identifier }
        if firstWithIdentifier == nil { firstWithIdentifier = identifier }
      }
      guard let parent = OwnerResolver.parent(of: current), parent > 1, parent != current else {
        break
      }
      current = parent
    }
    return firstWithIdentifier
  }

  private static func parent(of pid: pid_t) -> pid_t? {
    var info = kinfo_proc()
    var size = MemoryLayout<kinfo_proc>.stride
    var name: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
    guard sysctl(&name, UInt32(name.count), &info, &size, nil, 0) == 0, size > 0 else { return nil }
    return info.kp_eproc.e_ppid
  }
}

// MARK: - Reading Core Audio

enum AudioProcesses {
  private static let system = AudioObjectID(kAudioObjectSystemObject)

  static func list(owners: OwnerResolver) -> [AudioProcess] {
    var address = globalAddress(kAudioHardwarePropertyProcessObjectList)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) == noErr, size > 0
    else { return [] }
    let stride = MemoryLayout<AudioObjectID>.stride
    var objects = [AudioObjectID](
      repeating: AudioObjectID(kAudioObjectUnknown), count: Int(size) / stride)
    let status = objects.withUnsafeMutableBufferPointer {
      AudioObjectGetPropertyData(system, &address, 0, nil, &size, $0.baseAddress!)
    }
    guard status == noErr else { return [] }

    var found: [AudioProcess] = []
    for object in objects.prefix(Int(size) / stride) {
      guard let pid: pid_t = number(object, kAudioProcessPropertyPID) else { continue }
      let bundleID = string(object, kAudioProcessPropertyBundleID) ?? ""
      let input: UInt32 = number(object, kAudioProcessPropertyIsRunningInput) ?? 0
      let output: UInt32 = number(object, kAudioProcessPropertyIsRunningOutput) ?? 0
      found.append(
        AudioProcess(
          pid: pid, bundleID: bundleID, owner: owners.owner(of: pid, fallback: bundleID),
          input: input != 0, output: output != 0))
    }
    owners.keep(only: Set(found.map(\.pid)))
    return found
  }

  static func number<T: FixedWidthInteger>(
    _ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
    scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
  ) -> T? {
    var address = AudioObjectPropertyAddress(
      mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var value: T = 0
    var size = UInt32(MemoryLayout<T>.size)
    guard AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value) == noErr else {
      return nil
    }
    return value
  }

  static func string(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var address = globalAddress(selector)
    var value: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.stride)
    let status = withUnsafeMutablePointer(to: &value) {
      AudioObjectGetPropertyData(object, &address, 0, nil, &size, $0)
    }
    return status == noErr ? value as String : nil
  }
}

/// For `Jarvis --list-audio`.
func printAudioTable() {
  let table = AudioProcesses.list(owners: OwnerResolver())
  func pad(_ text: String, _ width: Int) -> String {
    text.padding(toLength: max(width, text.count), withPad: " ", startingAt: 0)
  }
  print("\(pad("PID", 7)) IN OUT  \(pad("BUNDLE ID", 44)) OWNER")
  for process in table.sorted(by: { $0.pid < $1.pid }) {
    let bundle = process.bundleID.isEmpty ? "-" : process.bundleID
    let owner = process.owner.isEmpty ? "-" : process.owner
    print(
      "\(pad(String(process.pid), 7)) \(process.input ? "1 " : "0 ") \(process.output ? "1 " : "0 ")"
        + "  \(pad(bundle, 44)) \(owner)")
  }
}

// MARK: - Headphones

enum OutputDevice {
  private static let headphoneSource: UInt32 = 0x6864_706E  // 'hdpn'
  private static let headsetWords = ["headset", "headphone", "earphone", "earbud"]

  /// True when the Mac plays through something the microphone cannot hear.
  static func isHeadphones() -> Bool {
    guard
      let device: AudioDeviceID = AudioProcesses.number(
        AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice),
      device != kAudioObjectUnknown
    else { return false }
    let transport: UInt32 = AudioProcesses.number(device, kAudioDevicePropertyTransportType) ?? 0
    let name = (AudioProcesses.string(device, kAudioObjectPropertyName) ?? "").lowercased()
    switch transport {
    case kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE:
      return true
    case kAudioDeviceTransportTypeBuiltIn:
      let source: UInt32? = AudioProcesses.number(
        device, kAudioDevicePropertyDataSource, scope: kAudioDevicePropertyScopeOutput)
      // Newer Macs show the headphone socket as its own device with no data source.
      return source == headphoneSource || name.contains("headphone")
    case kAudioDeviceTransportTypeUSB:
      return headsetWords.contains { name.contains($0) }
    default:
      return false
    }
  }
}

enum InputDevice {
  private static let system = AudioObjectID(kAudioObjectSystemObject)
  private static let internalMicrophone: UInt32 = 0x696D_6963  // 'imic', the data source

  /// The input macOS has made the default: what an app records from unless it asks for another.
  /// (A test copy can be told to see another device as the default, to try each case of the
  /// rule for choosing a microphone without changing the Mac's settings.)
  static func systemDefault() -> AudioDeviceID? {
    if let part = TestMode.text("JARVIS_TEST_DEFAULT_INPUT") { return find(named: part) }
    guard
      let device: AudioDeviceID = AudioProcesses.number(
        system, kAudioHardwarePropertyDefaultInputDevice),
      device != kAudioObjectUnknown
    else { return nil }
    return device
  }

  /// The test copy's own pick, from JARVIS_INPUT_DEVICE (part of a device name); nil when that
  /// is not set. If no input device has that name the answer is `kAudioObjectUnknown`, so a
  /// test never falls back to, say, the user's earphones.
  static func testChoice() -> AudioDeviceID? {
    guard let part = TestMode.text("JARVIS_INPUT_DEVICE") else { return nil }
    return find(named: part) ?? AudioDeviceID(kAudioObjectUnknown)
  }

  /// True for a Bluetooth microphone (AirPods and the like). Opening one switches the earphones
  /// to their call mode, which the wearer hears, and takes a second or two.
  static func isBluetooth(_ device: AudioDeviceID) -> Bool {
    let transport: UInt32 = AudioProcesses.number(device, kAudioDevicePropertyTransportType) ?? 0
    return transport == kAudioDeviceTransportTypeBluetooth
      || transport == kAudioDeviceTransportTypeBluetoothLE
  }

  static func name(of device: AudioDeviceID?) -> String {
    guard let device, device != kAudioObjectUnknown else { return "none" }
    return AudioProcesses.string(device, kAudioObjectPropertyName) ?? "unnamed"
  }

  /// The format sound arrives in from the device's first input stream.
  static func streamFormat(of device: AudioDeviceID) -> AudioStreamBasicDescription? {
    var streamsAddress = AudioObjectPropertyAddress(
      mSelector: kAudioDevicePropertyStreams, mScope: kAudioDevicePropertyScopeInput,
      mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(device, &streamsAddress, 0, nil, &size) == noErr,
      size >= UInt32(MemoryLayout<AudioStreamID>.size)
    else { return nil }
    var streams = [AudioStreamID](
      repeating: AudioStreamID(kAudioObjectUnknown),
      count: Int(size) / MemoryLayout<AudioStreamID>.stride)
    guard AudioObjectGetPropertyData(device, &streamsAddress, 0, nil, &size, &streams) == noErr,
      let stream = streams.first
    else { return nil }
    var formatAddress = globalAddress(kAudioStreamPropertyVirtualFormat)
    var description = AudioStreamBasicDescription()
    var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.stride)
    guard
      AudioObjectGetPropertyData(stream, &formatAddress, 0, nil, &formatSize, &description)
        == noErr
    else { return nil }
    return description
  }

  /// The Mac's own microphone, if there is one that can be used now. With the lid closed (an
  /// external display in use) macOS takes it away, and some Macs have none: then this is nil.
  static func builtIn() -> AudioDeviceID? {
    if TestMode.flag("JARVIS_TEST_NO_BUILTIN") { return nil }
    let usable = inputDevices().filter { device in
      let transport: UInt32 =
        AudioProcesses.number(device, kAudioDevicePropertyTransportType) ?? 0
      let alive: UInt32 = AudioProcesses.number(device, kAudioDevicePropertyDeviceIsAlive) ?? 0
      let hidden: UInt32 = AudioProcesses.number(device, kAudioDevicePropertyIsHidden) ?? 0
      // Hog mode: another process may have the device to itself, and then it is not ours to use.
      let hog: Int32 = AudioProcesses.number(device, kAudioDevicePropertyHogMode) ?? -1
      // A microphone macOS would not let anyone make the default is not one to use either.
      let selectable: UInt32 =
        AudioProcesses.number(
          device, kAudioDevicePropertyDeviceCanBeDefaultDevice,
          scope: kAudioDevicePropertyScopeInput) ?? 1
      return transport == kAudioDeviceTransportTypeBuiltIn && alive != 0 && hidden == 0
        && selectable != 0 && (hog == -1 || hog == getpid())
    }
    // The microphone in the case, rather than, say, a headset on the headphone socket.
    return usable.first { device in
      let source: UInt32? = AudioProcesses.number(
        device, kAudioDevicePropertyDataSource, scope: kAudioDevicePropertyScopeInput)
      return source == internalMicrophone
    } ?? usable.first
  }

  /// The first input device whose name contains `part`, ignoring case.
  static func find(named part: String) -> AudioDeviceID? {
    let wanted = part.lowercased()
    return inputDevices().first {
      (AudioProcesses.string($0, kAudioObjectPropertyName) ?? "").lowercased().contains(wanted)
    }
  }

  /// Every device that can record.
  private static func inputDevices() -> [AudioDeviceID] {
    var address = globalAddress(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) == noErr, size > 0
    else { return [] }
    let stride = MemoryLayout<AudioDeviceID>.stride
    var devices = [AudioDeviceID](
      repeating: AudioDeviceID(kAudioObjectUnknown), count: Int(size) / stride)
    let status = devices.withUnsafeMutableBufferPointer {
      AudioObjectGetPropertyData(system, &address, 0, nil, &size, $0.baseAddress!)
    }
    guard status == noErr else { return [] }
    return devices.filter(hasInput)
  }

  private static func hasInput(_ device: AudioDeviceID) -> Bool {
    var address = AudioObjectPropertyAddress(
      mSelector: kAudioDevicePropertyStreams, mScope: kAudioDevicePropertyScopeInput,
      mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    return AudioObjectGetPropertyDataSize(device, &address, 0, nil, &size) == noErr && size > 0
  }
}

// MARK: - The call state

final class CallMonitor {
  static let pollsToStart = 2
  static let secondsToEnd: TimeInterval = 12

  var callApps: [String]
  var onChange: ((_ active: Bool, _ app: String?) -> Void)?
  private(set) var active = false
  private(set) var app: String?

  private let owners = OwnerResolver()
  private let ownBundleID = Bundle.main.bundleIdentifier ?? "com.ajay.jarvis"
  // Test hook: read the table from a JSON file instead of Core Audio.
  private let fakeTable = ProcessInfo.processInfo.environment["JARVIS_FAKE_AUDIO"]
  private var candidate: String?
  private var streak = 0
  private var lastHeard = Date.distantPast

  init(callApps: [String]) {
    self.callApps = callApps
  }

  func poll(now: Date = Date()) {
    let table = fakeTable.map(CallMonitor.readTable) ?? AudioProcesses.list(owners: owners)
    evaluate(table, now: now)
  }

  func evaluate(_ table: [AudioProcess], now: Date) {
    let others = table.filter { !isOurs($0) }
    if active {
      // Muting can close the microphone, so sound in either direction keeps the call on.
      if others.contains(where: { callKey($0) == app && ($0.input || $0.output) }) {
        lastHeard = now
      } else if now.timeIntervalSince(lastHeard) >= CallMonitor.secondsToEnd {
        let ended = app ?? "?"
        active = false
        app = nil
        candidate = nil
        streak = 0
        Log.write("call ended: \(ended)")
        onChange?(false, nil)
      }
      return
    }

    guard let hit = others.first(where: { $0.input && callKey($0) != nil }),
      let found = callKey(hit)
    else {
      candidate = nil
      streak = 0
      return
    }
    streak = candidate == found ? streak + 1 : 1
    candidate = found
    if streak >= CallMonitor.pollsToStart {
      active = true
      app = found
      lastHeard = now
      Log.write("call started: \(found) (pid \(hit.pid), \(hit.bundleID))")
      onChange?(true, found)
    }
  }

  /// The call app a process counts towards, or nil when it is not one.
  private func callKey(_ process: AudioProcess) -> String? {
    if isCallApp(process.owner) { return process.owner }
    if isCallApp(process.bundleID) { return process.bundleID }
    return nil
  }

  private func isCallApp(_ identifier: String) -> Bool {
    !identifier.isEmpty && callApps.contains { identifier.hasPrefix($0) }
  }

  /// Jarvis's own capture must never look like a call.
  private func isOurs(_ process: AudioProcess) -> Bool {
    process.pid == getpid() || process.bundleID == ownBundleID || process.owner == ownBundleID
  }

  private static func readTable(_ path: String) -> [AudioProcess] {
    guard let data = FileManager.default.contents(atPath: path),
      let rows = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]]
    else { return [] }
    return rows.map { row in
      let bundle = row["bundle"] as? String ?? ""
      return AudioProcess(
        pid: pid_t(row["pid"] as? Int ?? 0), bundleID: bundle,
        owner: row["owner"] as? String ?? bundle,
        input: row["in"] as? Bool ?? false, output: row["out"] as? Bool ?? false)
    }
  }
}
