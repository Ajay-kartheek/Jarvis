// Jarvis
//
// One program, started in one of three ways:
//   Jarvis                                     the menu-bar app (App.swift)
//                                              JARVIS_TEST=1 makes it a test copy (see TestMode)
//   Jarvis --port N [--no-mic] [--no-system]   the audio helper (Capture.swift)
//   Jarvis --list-audio                        print which processes use audio, then exit

import Foundation

if CommandLine.arguments.contains("--port") {
  guard let portText = argumentValue("--port"), let port = UInt16(portText) else {
    fputs("usage: Jarvis --port <tcp port on 127.0.0.1> [--no-mic] [--no-system]\n", stderr)
    exit(2)
  }
  runCaptureHelper(port: port)
} else if CommandLine.arguments.contains("--list-audio") {
  printAudioTable()
  exit(0)
} else {
  runApp()
}
