import Foundation
import Speech

struct Input: Decodable {
    let audioPath: String
    let language: String
    enum CodingKeys: String, CodingKey { case audioPath = "audio_path", language }
}

func emit(_ value: [String: String]) {
    guard let data = try? JSONSerialization.data(withJSONObject: value),
          let line = String(data: data, encoding: .utf8) else { return }
    print(line)
    fflush(stdout)
}

func fail(_ reason: String) -> Never {
    emit(["type": "error", "error": reason])
    exit(2)
}

guard let line = readLine(), let data = line.data(using: .utf8),
      let input = try? JSONDecoder().decode(Input.self, from: data) else {
    fail("MACOS_SPEECH_REQUEST_INVALID")
}
let localeID = input.language == "ja" ? "ja-JP" : input.language == "en" ? "en-US" : ""
guard !localeID.isEmpty else { fail("MACOS_SPEECH_LANGUAGE_UNSUPPORTED") }

var authorization = SFSpeechRecognizer.authorizationStatus()
if authorization == .notDetermined {
    let ready = DispatchSemaphore(value: 0)
    SFSpeechRecognizer.requestAuthorization { status in authorization = status; ready.signal() }
    if ready.wait(timeout: .now() + 30) == .timedOut { fail("MACOS_SPEECH_AUTHORIZATION_TIMEOUT") }
}
guard authorization == .authorized else { fail("MACOS_SPEECH_PERMISSION_DENIED") }
guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeID)), recognizer.isAvailable else {
    fail("MACOS_SPEECH_UNAVAILABLE")
}
guard recognizer.supportsOnDeviceRecognition else { fail("MACOS_SPEECH_ON_DEVICE_UNAVAILABLE") }

let request = SFSpeechURLRecognitionRequest(url: URL(fileURLWithPath: input.audioPath))
request.shouldReportPartialResults = true
request.requiresOnDeviceRecognition = true
let finished = DispatchSemaphore(value: 0)
let lock = NSLock()
var previous = ""
var didFinish = false
let task = recognizer.recognitionTask(with: request) { result, error in
    lock.lock()
    defer { lock.unlock() }
    if let result {
        let text = result.bestTranscription.formattedString
        if !text.isEmpty && text != previous {
            previous = text
            emit(["type": result.isFinal ? "done" : "text", "text": text])
        }
        if result.isFinal && !didFinish { didFinish = true; finished.signal() }
    }
    if let error, !didFinish {
        didFinish = true
        emit(["type": "error", "error": "MACOS_SPEECH_RECOGNITION_FAILED", "message": error.localizedDescription])
        finished.signal()
    }
}
if finished.wait(timeout: .now() + 90) == .timedOut {
    task.cancel()
    fail("MACOS_SPEECH_TIMEOUT")
}
if previous.isEmpty { fail("MACOS_SPEECH_NO_SPEECH") }
