import Foundation
import EventKit
import CryptoKit
import AppKit

struct Failure: Error { let code: String }
func fail(_ code: String) throws -> Never { throw Failure(code: code) }
func output(_ value: [String: Any]) {
    let data = (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data("{}".utf8)
    FileHandle.standardOutput.write(data); FileHandle.standardOutput.write(Data("\n".utf8))
}
final class AuthorizationReceipt: @unchecked Sendable {
    private let lock = NSLock()
    private var granted = false
    private var failed = false
    func complete(_ allowed: Bool, _ error: Error?) {
        lock.lock(); defer { lock.unlock() }
        granted = allowed; failed = error != nil
    }
    func result() -> (Bool, Bool) {
        lock.lock(); defer { lock.unlock() }
        return (granted, failed)
    }
}
func authorized() -> Bool {
    let value = EKEventStore.authorizationStatus(for: .event)
    if #available(macOS 14.0, *) { return value == .fullAccess }
    return value == .authorized
}
func permission() -> String {
    switch EKEventStore.authorizationStatus(for: .event) {
    case .notDetermined: return "not_determined"
    case .denied: return "denied"
    case .restricted: return "restricted"
    default: return authorized() ? "authorized" : "write_only"
    }
}
func status() -> [String: Any] {
    return ["available": true, "authorized": authorized(), "permission": permission(), "provider": "apple"]
}
func calendarIcon() throws -> [String: Any] {
    guard let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.iCal"),
        let tiff = NSWorkspace.shared.icon(forFile: app.path).tiffRepresentation,
        let bitmap = NSBitmapImageRep(data: tiff),
        let png = bitmap.representation(using: .png, properties: [:]) else {
        try fail("CALENDAR_ICON_UNAVAILABLE")
    }
    return ["png_base64": png.base64EncodedString()]
}
func date(_ value: String, zone: TimeZone) -> Date? {
    let iso = ISO8601DateFormatter()
    iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let parsed = iso.date(from: value) { return parsed }
    iso.formatOptions = [.withInternetDateTime]
    if let parsed = iso.date(from: value) { return parsed }
    let plain = DateFormatter(); plain.locale = Locale(identifier: "en_US_POSIX")
    plain.timeZone = zone; plain.dateFormat = "yyyy-MM-dd"; plain.isLenient = false
    return value.count == 10 ? plain.date(from: value) : nil
}
func iso(_ value: Date) -> String { ISO8601DateFormatter().string(from: value) }
func token(_ event: EKEvent) -> String {
    let value: [String: Any] = ["identifier": event.eventIdentifier ?? event.calendarItemIdentifier,
        "start": event.startDate.timeIntervalSince1970, "calendar": event.calendar.calendarIdentifier]
    let raw = (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data()
    return raw.base64EncodedString().replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
}
func publicEvent(_ event: EKEvent) -> [String: Any] {
    let zone = event.timeZone ?? .current
    func boundary(_ value: Date) -> [String: Any] {
        if event.isAllDay {
            let format = DateFormatter(); format.locale = Locale(identifier: "en_US_POSIX")
            format.timeZone = zone; format.dateFormat = "yyyy-MM-dd"
            return ["date": format.string(from: value)]
        }
        return ["dateTime": iso(value), "timeZone": zone.identifier]
    }
    var value: [String: Any] = ["id": token(event), "summary": event.title ?? "",
        "start": boundary(event.startDate), "end": boundary(event.endDate), "provider": "apple", "location": event.location ?? "", "description": event.notes ?? "", "url": event.url?.absoluteString ?? ""]
    let fingerprint: [String: Any] = ["event": value, "modified": event.lastModifiedDate?.timeIntervalSince1970 ?? 0,
        "location": event.location ?? "", "notes": event.notes ?? "", "url": event.url?.absoluteString ?? ""]
    let raw = (try? JSONSerialization.data(withJSONObject: fingerprint, options: [.sortedKeys])) ?? Data()
    value["etag"] = SHA256.hash(data: raw).map { String(format: "%02x", $0) }.joined()
    return value
}
func calendar(_ store: EKEventStore, _ identifier: String) throws -> EKCalendar {
    let result = identifier == "primary" ? store.defaultCalendarForNewEvents : store.calendar(withIdentifier: identifier)
    guard let value = result else { try fail("CALENDAR_NO_CALENDARS") }
    return value
}
func findEvent(_ store: EKEventStore, _ identifier: String, _ chosen: EKCalendar) throws -> EKEvent {
    var padded = identifier.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    padded += String(repeating: "=", count: (4 - padded.count % 4) % 4)
    guard let raw = Data(base64Encoded: padded), let value = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
          let native = value["identifier"] as? String, let start = value["start"] as? Double,
          value["calendar"] as? String == chosen.calendarIdentifier else { try fail("CALENDAR_EVENT_NOT_FOUND") }
    let when = Date(timeIntervalSince1970: start)
    let predicate = store.predicateForEvents(withStart: when.addingTimeInterval(-1), end: when.addingTimeInterval(1), calendars: [chosen])
    guard let event = store.events(matching: predicate).first(where: {
        ($0.eventIdentifier == native || $0.calendarItemIdentifier == native) && abs($0.startDate.timeIntervalSince1970 - start) < 0.5
    }) else { try fail("CALENDAR_EVENT_NOT_FOUND") }
    return event
}
func run(_ input: [String: Any]) throws -> [String: Any] {
    let operation = input["operation"] as? String ?? "status"
    if operation == "status" { return status() }
    if operation == "icon" { return try calendarIcon() }
    if operation == "self_test" {
        guard let zone = TimeZone(identifier: "Asia/Tokyo"),
            date("2026-10-28T15:00:00+09:00", zone: zone) != nil,
            date("2026-10-28", zone: zone) != nil, date("invalid", zone: zone) == nil else { try fail("CALENDAR_SELF_TEST_FAILED") }
        return ["ok": true]
    }
    let store = EKEventStore()
    if operation == "connect" {
        if !authorized() {
            let receipt = AuthorizationReceipt()
            let done = DispatchSemaphore(value: 0)
            let completion: @Sendable (Bool, Error?) -> Void = { granted, error in
                receipt.complete(granted, error); done.signal()
            }
            if #available(macOS 14.0, *) { store.requestFullAccessToEvents(completion: completion) }
            else { store.requestAccess(to: .event, completion: completion) }
            let deadline = Date().addingTimeInterval(120)
            while done.wait(timeout: .now()) != .success {
                guard Date() < deadline else { try fail("CALENDAR_AUTH_TIMEOUT") }
                RunLoop.current.run(until: Date().addingTimeInterval(0.05))
            }
            let (granted, failed) = receipt.result()
            if !granted || failed {
                let denied = ["denied", "restricted", "write_only"].contains(permission())
                try fail(denied ? "CALENDAR_ACCESS_DENIED" : "MACOS_CALENDAR_AUTH_FAILED")
            }
        }
        guard authorized() else { try fail("CALENDAR_ACCESS_DENIED") }
        return status()
    }
    guard authorized() else { try fail("CALENDAR_PERMISSION_DENIED") }
    if operation == "calendars" {
        let primary = store.defaultCalendarForNewEvents?.calendarIdentifier
        return ["calendars": store.calendars(for: .event).map {
            ["id": $0.calendarIdentifier, "title": $0.title, "writable": $0.allowsContentModifications,
             "primary": $0.calendarIdentifier == primary, "source": $0.source.title] as [String: Any]
        }]
    }
    let chosen = try calendar(store, input["calendar_id"] as? String ?? "primary")
    let zone = TimeZone(identifier: input["time_zone"] as? String ?? "") ?? .current
    if operation == "events" {
        guard let start = date(input["start"] as? String ?? "", zone: zone),
            let end = date(input["end"] as? String ?? "", zone: zone), end > start,
            end.timeIntervalSince(start) <= 366 * 86400 else { try fail("CALENDAR_RANGE_INVALID") }
        let predicate = store.predicateForEvents(withStart: start, end: end, calendars: [chosen])
        let query = input["query"] as? String ?? ""
        let events = store.events(matching: predicate).filter { query.isEmpty || ($0.title ?? "").localizedCaseInsensitiveContains(query) }
            .sorted { $0.startDate < $1.startDate }
        return ["events": events.prefix(100).map(publicEvent), "more": events.count > 100]
    }
    guard ["get", "create", "update", "delete"].contains(operation) else { try fail("CALENDAR_REQUEST_FAILED") }
    let event = operation == "create" ? EKEvent(eventStore: store) : try findEvent(store, input["event_id"] as? String ?? "", chosen)
    if operation == "get" { return publicEvent(event) }
    guard chosen.allowsContentModifications else { try fail("CALENDAR_PERMISSION_DENIED") }
    if operation != "create" {
        guard let expected = input["etag"] as? String, expected == publicEvent(event)["etag"] as? String else { try fail("CALENDAR_EVENT_CHANGED") }
    }
    if operation == "delete" {
        let result = publicEvent(event); try store.remove(event, span: .thisEvent, commit: true); return result
    }
    guard let payload = input["payload"] as? [String: Any], let title = payload["summary"] as? String,
        !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, title.count <= 200,
        let startValue = payload["start"] as? [String: Any], let endValue = payload["end"] as? [String: Any] else { try fail("CALENDAR_EVENT_INVALID") }
    let eventZone = TimeZone(identifier: startValue["timeZone"] as? String ?? "") ?? zone
    guard let start = date((startValue["dateTime"] ?? startValue["date"]) as? String ?? "", zone: eventZone),
        let end = date((endValue["dateTime"] ?? endValue["date"]) as? String ?? "", zone: eventZone),
        end > start, end.timeIntervalSince(start) <= 366 * 86400 else { try fail("CALENDAR_TIME_REQUIRED") }
    if operation == "create", let operationID = payload["id"] as? String,
       operationID.hasPrefix("olcr"),
       let marker = payload["description"] as? String,
       marker == "OLCR medication occurrence " + operationID {
        let predicate = store.predicateForEvents(withStart: start.addingTimeInterval(-1), end: end.addingTimeInterval(1), calendars: [chosen])
        let existing = store.events(matching: predicate).filter { $0.notes == marker }
        if existing.count > 1 { try fail("CALENDAR_EVENT_CHANGED") }
        if let saved = existing.first { return publicEvent(saved) }
    }
    if let reminders = payload["reminders"] as? [String: Any] {
        guard let overrides = reminders["overrides"] as? [[String: Any]], overrides.count <= 5 else { try fail("CALENDAR_EVENT_INVALID") }
        var alarms: [EKAlarm] = []
        for reminder in overrides {
            guard reminder["method"] as? String == "popup", let minutes = reminder["minutes"] as? Int,
                  minutes >= 0, minutes <= 40320 else { try fail("CALENDAR_EVENT_INVALID") }
            alarms.append(EKAlarm(relativeOffset: -Double(minutes) * 60))
        }
        event.alarms = alarms
    }
    event.calendar = chosen; event.title = title; event.startDate = start; event.endDate = end
    event.isAllDay = startValue["date"] != nil; event.timeZone = eventZone
    if let location = payload["location"] as? String { event.location = location }
    if let notes = payload["description"] as? String { event.notes = notes }
    if let rawURL = payload["url"] as? String {
        if rawURL.isEmpty { event.url = nil }
        else {
            guard let url = URL(string: rawURL), ["https", "http"].contains(url.scheme?.lowercased() ?? ""), url.host != nil else { try fail("CALENDAR_EVENT_INVALID") }
            event.url = url
        }
    }
    try store.save(event, span: .thisEvent, commit: true)
    return publicEvent(event)
}
do {
    let raw = FileHandle.standardInput.readDataToEndOfFile()
    guard raw.count <= 1_000_000, let input = try JSONSerialization.jsonObject(with: raw) as? [String: Any] else { try fail("CALENDAR_EVENT_INVALID") }
    output(try run(input))
} catch let failure as Failure { output(["error": failure.code]); exit(1) }
catch { output(["error": "CALENDAR_REQUEST_FAILED"]); exit(1) }
