// JarvisEars
//
// Captures the two sides of a call on this Mac and streams them to the Jarvis
// Python process as 16 kHz mono float32 PCM over a local TCP socket:
//   - system audio (what the other people say), through a Core Audio process tap
//   - the microphone (what you say), through AVAudioEngine
//
// Nothing is written to disk here and nothing leaves the Mac.
//
// The tap plus tap-only aggregate device recipe follows audiotee
// (https://github.com/makeusabrew/audiotee, MIT).
//
// Usage: JarvisEars --port <tcp port on 127.0.0.1> [--no-mic] [--no-system]

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

let targetSampleRate: Double = 16_000
let levelReportInterval: TimeInterval = 2

// MARK: - Wire format
//
// Packet = 8-byte header + payload.
//   bytes 0-1  magic "JE"
//   byte  2    kind: 0 = system audio, 1 = microphone, 2 = status (UTF-8 JSON)
//   byte  3    reserved, 0
//   bytes 4-7  payload length in bytes, little-endian
// Audio payload: float32 little-endian samples, 16 kHz, mono.

enum PacketKind: UInt8 {
  case system = 0
  case mic = 1
  case status = 2
}

struct EarsError: Error, CustomStringConvertible {
  let description: String
  init(_ description: String) { self.description = description }
}

func check(_ status: OSStatus, _ what: String) throws {
  guard status == noErr else { throw EarsError("\(what) failed (OSStatus \(status))") }
}

func globalAddress(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
  AudioObjectPropertyAddress(
    mSelector: selector,
    mScope: kAudioObjectPropertyScopeGlobal,
    mElement: kAudioObjectPropertyElementMain)
}

// MARK: - Link to the Python process

final class Link {
  private let fd: Int32
  private let queue = DispatchQueue(label: "jarvis.ears.link")

  init(port: UInt16) throws {
    fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { throw EarsError("socket() failed (errno \(errno))") }
    var one: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, socklen_t(MemoryLayout<Int32>.size))
    var address = sockaddr_in()
    address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    address.sin_family = sa_family_t(AF_INET)
    address.sin_port = port.bigEndian
    address.sin_addr.s_addr = inet_addr("127.0.0.1")
    let result = withUnsafePointer(to: &address) {
      $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
      }
    }
    guard result == 0 else {
      throw EarsError("cannot connect to 127.0.0.1:\(port) (errno \(errno))")
    }
  }

  func send(_ kind: PacketKind, _ payload: Data) {
    queue.async { [fd] in
      var header = Data([0x4A, 0x45, kind.rawValue, 0])
      var length = UInt32(payload.count).littleEndian
      withUnsafeBytes(of: &length) { header.append(contentsOf: $0) }
      if !Link.writeAll(fd, header) || !Link.writeAll(fd, payload) {
        // The Python side went away, so there is nobody left to listen for.
        exit(0)
      }
    }
  }

  func status(_ event: String, _ fields: [String: Any] = [:]) {
    var body = fields
    body["event"] = event
    if let data = try? JSONSerialization.data(withJSONObject: body) {
      send(.status, data)
    }
  }

  /// Blocks until everything queued so far has been written.
  func flush() { queue.sync {} }

  private static func writeAll(_ fd: Int32, _ data: Data) -> Bool {
    data.withUnsafeBytes { raw -> Bool in
      var offset = 0
      while offset < raw.count {
        let written = write(fd, raw.baseAddress! + offset, raw.count - offset)
        if written < 0 && errno == EINTR { continue }
        if written <= 0 { return false }
        offset += written
      }
      return true
    }
  }
}

// MARK: - Conversion to 16 kHz mono float32

final class Resampler {
  private let converter: AVAudioConverter
  private let inputFormat: AVAudioFormat
  private let outputFormat: AVAudioFormat

  init?(from inputFormat: AVAudioFormat) {
    guard
      let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32, sampleRate: targetSampleRate, channels: 1,
        interleaved: false),
      let converter = AVAudioConverter(from: inputFormat, to: outputFormat)
    else { return nil }
    converter.downmix = inputFormat.channelCount > 1
    self.converter = converter
    self.inputFormat = inputFormat
    self.outputFormat = outputFormat
  }

  /// Returns the converted samples and their peak level, or nil if there was nothing to convert.
  func convert(_ buffer: AVAudioPCMBuffer) -> (data: Data, peak: Float)? {
    let ratio = outputFormat.sampleRate / inputFormat.sampleRate
    let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
    guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
      return nil
    }
    var consumed = false
    var conversionError: NSError?
    let status = converter.convert(to: output, error: &conversionError) { _, inputStatus in
      // Hand the buffer over once; "no data now" keeps the converter's state
      // for the next call, which "end of stream" would not.
      if consumed {
        inputStatus.pointee = .noDataNow
        return nil
      }
      consumed = true
      inputStatus.pointee = .haveData
      return buffer
    }
    guard status != .error, output.frameLength > 0, let channels = output.floatChannelData else {
      return nil
    }
    let count = Int(output.frameLength)
    var peak: Float = 0
    for index in 0..<count {
      let magnitude = abs(channels[0][index])
      if magnitude > peak { peak = magnitude }
    }
    return (Data(bytes: channels[0], count: count * MemoryLayout<Float>.size), peak)
  }
}

/// Reports the loudest sample seen every couple of seconds, so the Python side
/// can tell real silence from a tap that macOS is feeding zeros (which is what
/// happens, with no error, when the audio permission is missing).
final class LevelMeter {
  private let link: Link
  private let source: String
  private var peak: Float = 0
  private var lastReport = Date()

  init(link: Link, source: String) {
    self.link = link
    self.source = source
  }

  func add(_ value: Float) {
    if value > peak { peak = value }
    let now = Date()
    if now.timeIntervalSince(lastReport) >= levelReportInterval {
      link.status("level", ["source": source, "peak": Double(peak)])
      peak = 0
      lastReport = now
    }
  }
}

// MARK: - System audio

final class SystemAudioTap {
  private let link: Link
  private let queue = DispatchQueue(label: "jarvis.ears.system", qos: .userInitiated)
  private var tapID = AudioObjectID(kAudioObjectUnknown)
  private var aggregateID = AudioObjectID(kAudioObjectUnknown)
  private var procID: AudioDeviceIOProcID?
  private var format: AVAudioFormat?
  private var resampler: Resampler?
  private let meter: LevelMeter
  private var outputListener: AudioObjectPropertyListenerBlock?

  init(link: Link) {
    self.link = link
    self.meter = LevelMeter(link: link, source: "system")
  }

  func start() throws {
    do {
      try build()
    } catch {
      teardown()
      throw error
    }
    watchDefaultOutputDevice()
  }

  private func build() throws {
    // A mono mix of everything every other process plays.
    let description = CATapDescription()
    description.name = "JarvisEars"
    description.processes = []
    description.isExclusive = true  // "all processes except the listed ones"
    description.isPrivate = true
    description.isMixdown = true
    description.isMono = true
    description.muteBehavior = .unmuted

    var newTap = AudioObjectID(kAudioObjectUnknown)
    try check(AudioHardwareCreateProcessTap(description, &newTap), "Creating the system-audio tap")
    tapID = newTap

    var formatAddress = globalAddress(kAudioTapPropertyFormat)
    var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.stride)
    var streamDescription = AudioStreamBasicDescription()
    try check(
      AudioObjectGetPropertyData(tapID, &formatAddress, 0, nil, &formatSize, &streamDescription),
      "Reading the tap format")
    guard let tapFormat = AVAudioFormat(streamDescription: &streamDescription),
      let resampler = Resampler(from: tapFormat)
    else {
      throw EarsError("Unsupported system-audio format")
    }
    format = tapFormat
    self.resampler = resampler

    // An aggregate device that holds only the tap gives us something to read from.
    let aggregate: [String: Any] = [
      kAudioAggregateDeviceNameKey: "JarvisEars",
      kAudioAggregateDeviceUIDKey: UUID().uuidString,
      kAudioAggregateDeviceSubDeviceListKey: [] as CFArray,
      kAudioAggregateDeviceMainSubDeviceKey: 0,
      kAudioAggregateDeviceIsPrivateKey: true,
      kAudioAggregateDeviceIsStackedKey: false,
    ]
    var newAggregate = AudioObjectID(kAudioObjectUnknown)
    try check(
      AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &newAggregate),
      "Creating the capture device")
    aggregateID = newAggregate

    var uidAddress = globalAddress(kAudioTapPropertyUID)
    var uidSize = UInt32(MemoryLayout<CFString>.stride)
    var tapUID: CFString = "" as CFString
    try check(
      withUnsafeMutablePointer(to: &tapUID) {
        AudioObjectGetPropertyData(tapID, &uidAddress, 0, nil, &uidSize, $0)
      }, "Reading the tap identifier")

    var listAddress = globalAddress(kAudioAggregateDevicePropertyTapList)
    let tapList = [tapUID] as CFArray
    try check(
      withUnsafePointer(to: tapList) {
        AudioObjectSetPropertyData(
          aggregateID, &listAddress, 0, nil, UInt32(MemoryLayout<CFArray>.stride), $0)
      }, "Attaching the tap to the capture device")

    var newProc: AudioDeviceIOProcID?
    try check(
      AudioDeviceCreateIOProcIDWithBlock(&newProc, aggregateID, queue) {
        [weak self] _, inputData, _, _, _ in
        self?.process(inputData)
      }, "Creating the audio callback")
    procID = newProc
    try check(AudioDeviceStart(aggregateID, procID), "Starting system-audio capture")

    link.status(
      "system_started",
      [
        "sample_rate": tapFormat.sampleRate,
        "channels": Int(tapFormat.channelCount),
      ])
  }

  private func process(_ inputData: UnsafePointer<AudioBufferList>) {
    guard let format, let resampler,
      let buffer = AVAudioPCMBuffer(
        pcmFormat: format, bufferListNoCopy: inputData, deallocator: nil),
      buffer.frameLength > 0,
      let converted = resampler.convert(buffer)
    else { return }
    meter.add(converted.peak)
    link.send(.system, converted.data)
  }

  private func teardown() {
    if aggregateID != kAudioObjectUnknown {
      if let procID {
        AudioDeviceStop(aggregateID, procID)
        AudioDeviceDestroyIOProcID(aggregateID, procID)
      }
      AudioHardwareDestroyAggregateDevice(aggregateID)
    }
    if tapID != kAudioObjectUnknown {
      AudioHardwareDestroyProcessTap(tapID)
    }
    procID = nil
    aggregateID = AudioObjectID(kAudioObjectUnknown)
    tapID = AudioObjectID(kAudioObjectUnknown)
    format = nil
    resampler = nil
  }

  /// Headphones plugged in or pulled out change the tap's format, so rebuild.
  private func watchDefaultOutputDevice() {
    var address = globalAddress(kAudioHardwarePropertyDefaultOutputDevice)
    let listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
      self?.rebuild(reason: "output device changed")
    }
    outputListener = listener
    AudioObjectAddPropertyListenerBlock(
      AudioObjectID(kAudioObjectSystemObject), &address, queue, listener)
  }

  private func rebuild(reason: String) {
    link.status("system_restarting", ["reason": reason])
    teardown()
    do {
      try build()
    } catch {
      teardown()
      link.status("system_failed", ["error": "\(error)"])
    }
  }

  func stop() {
    queue.sync { teardown() }
  }
}

// MARK: - Microphone

final class MicCapture {
  private let link: Link
  private let engine = AVAudioEngine()
  private var resampler: Resampler?
  private let meter: LevelMeter
  private var observer: NSObjectProtocol?

  init(link: Link) {
    self.link = link
    self.meter = LevelMeter(link: link, source: "mic")
  }

  func start() throws {
    try install()
    // The engine stops itself when the input device changes (AirPods connect,
    // a headset is unplugged), so set it up again.
    observer = NotificationCenter.default.addObserver(
      forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
    ) { [weak self] _ in
      guard let self else { return }
      self.link.status("mic_restarting", ["reason": "input device changed"])
      self.engine.inputNode.removeTap(onBus: 0)
      do {
        try self.install()
      } catch {
        self.link.status("mic_failed", ["error": "\(error)"])
      }
    }
  }

  private func install() throws {
    let input = engine.inputNode
    let format = input.outputFormat(forBus: 0)
    guard format.sampleRate > 0, format.channelCount > 0 else {
      throw EarsError("No microphone input is available")
    }
    guard let resampler = Resampler(from: format) else {
      throw EarsError("Unsupported microphone format")
    }
    self.resampler = resampler
    input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buffer, _ in
      guard let self, let converted = self.resampler?.convert(buffer) else { return }
      self.meter.add(converted.peak)
      self.link.send(.mic, converted.data)
    }
    engine.prepare()
    try engine.start()
    link.status(
      "mic_started",
      [
        "sample_rate": format.sampleRate,
        "channels": Int(format.channelCount),
      ])
  }

  func stop() {
    engine.inputNode.removeTap(onBus: 0)
    engine.stop()
  }
}

func microphoneAllowed() -> Bool {
  switch AVCaptureDevice.authorizationStatus(for: .audio) {
  case .authorized:
    return true
  case .notDetermined:
    let done = DispatchSemaphore(value: 0)
    var granted = false
    AVCaptureDevice.requestAccess(for: .audio) { allowed in
      granted = allowed
      done.signal()
    }
    done.wait()
    return granted
  default:
    return false
  }
}

// MARK: - Main

func argumentValue(_ name: String) -> String? {
  let arguments = CommandLine.arguments
  guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
  return arguments[index + 1]
}

guard let portText = argumentValue("--port"), let port = UInt16(portText) else {
  fputs("usage: JarvisEars --port <tcp port on 127.0.0.1> [--no-mic] [--no-system]\n", stderr)
  exit(2)
}

let link: Link
do {
  link = try Link(port: port)
} catch {
  fputs("JarvisEars: \(error)\n", stderr)
  exit(1)
}
link.status("hello", ["version": 1, "pid": Int(getpid()), "sample_rate": targetSampleRate])

var systemTap: SystemAudioTap?
var micCapture: MicCapture?

if !CommandLine.arguments.contains("--no-system") {
  let tap = SystemAudioTap(link: link)
  do {
    try tap.start()
    systemTap = tap
  } catch {
    link.status("system_failed", ["error": "\(error)"])
  }
}

if !CommandLine.arguments.contains("--no-mic") {
  if microphoneAllowed() {
    let capture = MicCapture(link: link)
    do {
      try capture.start()
      micCapture = capture
    } catch {
      link.status("mic_failed", ["error": "\(error)"])
    }
  } else {
    link.status("mic_denied")
  }
}

if systemTap == nil && micCapture == nil {
  link.status("nothing_started")
  link.flush()
  exit(1)
}

// Stop cleanly when asked to; a closed socket also ends the process (see Link.send).
signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let signalQueue = DispatchQueue(label: "jarvis.ears.signals")
let signalSources = [SIGTERM, SIGINT].map { number -> DispatchSourceSignal in
  let source = DispatchSource.makeSignalSource(signal: number, queue: signalQueue)
  source.setEventHandler {
    micCapture?.stop()
    systemTap?.stop()
    link.status("stopped")
    link.flush()
    exit(0)
  }
  source.resume()
  return source
}

dispatchMain()
