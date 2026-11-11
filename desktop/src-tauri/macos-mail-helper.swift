import Foundation
import AppKit
import Carbon

struct Failure: Error { let code: String }
func fail(_ code: String) throws -> Never { throw Failure(code: code) }
func output(_ value: [String: Any]) {
    let data = (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data("{}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func mailAppURL() -> URL? {
    NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.mail")
}

func automationPermission(askUser: Bool = false) -> OSStatus {
    let bundleID = "com.apple.mail"
    var target = AEAddressDesc()
    let created = bundleID.withCString { raw in
        AECreateDesc(DescType(typeApplicationBundleID), raw, bundleID.utf8.count, &target)
    }
    guard created == noErr else { return OSStatus(created) }
    defer { AEDisposeDesc(&target) }
    return AEDeterminePermissionToAutomateTarget(&target, AEEventClass(kCoreEventClass), AEEventID(kAEGetData), askUser)
}

func automationStatus(available: Bool, permission: OSStatus) -> [String: Any] {
    let state: String
    if !available { state = "unavailable" }
    else {
        switch permission {
        case noErr: state = "authorized"
        case -1744: state = "not_determined"
        case -1743: state = "denied"
        case -600: state = "not_running"
        default: state = "error"
        }
    }
    return ["available": available, "authorized": available && permission == noErr,
            "permission": state, "automation_status": Int(permission), "provider": "apple"]
}

func status() -> [String: Any] {
    let available = mailAppURL() != nil
    return automationStatus(available: available, permission: available ? automationPermission() : OSStatus(-600))
}

func requestMailAuthorization(available: Bool, checker: (Bool) -> OSStatus = { automationPermission(askUser: $0) }) throws -> [String: Any] {
    guard available else { try fail("APPLE_MAIL_UNAVAILABLE") }
    // Reading Mail's version does not establish automation consent. Request
    // consent explicitly for the same event class used by real Mail operations.
    let permission = checker(true)
    switch permission {
    case noErr: return automationStatus(available: true, permission: permission)
    case -1743: try fail("APPLE_MAIL_AUTOMATION_DENIED")
    case -1744: try fail("APPLE_MAIL_AUTOMATION_PERMISSION_REQUIRED")
    case -600: try fail("APPLE_MAIL_NOT_RUNNING")
    default: try fail("APPLE_MAIL_PERMISSION_CHECK_FAILED")
    }
}

func ensureMailRunning() throws {
    if NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.mail").contains(where: { !$0.isTerminated }) { return }
    guard let url = mailAppURL() else { try fail("APPLE_MAIL_UNAVAILABLE") }
    let configuration = NSWorkspace.OpenConfiguration()
    configuration.activates = false
    configuration.addsToRecentItems = false
    var finished = false
    var launched = false
    NSWorkspace.shared.openApplication(at: url, configuration: configuration) { application, error in
        launched = application != nil && error == nil
        finished = true
    }
    let deadline = Date().addingTimeInterval(15)
    while !finished && Date() < deadline {
        _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
    }
    guard finished && launched else { try fail("APPLE_MAIL_NOT_RUNNING") }
}

func executeScript(_ source: String) throws -> NSAppleEventDescriptor {
    guard let script = NSAppleScript(source: source) else { try fail("APPLE_MAIL_SCRIPT_INVALID") }
    var details: NSDictionary?
    let result = script.executeAndReturnError(&details)
    if let details {
        let number = details["NSAppleScriptErrorNumber"] as? Int ?? 0
        let message = details["NSAppleScriptErrorMessage"] as? String ?? ""
        if message.contains("APPLE_MAIL_MESSAGE_NOT_FOUND") { try fail("APPLE_MAIL_MESSAGE_NOT_FOUND") }
        if number == -1743 { try fail("APPLE_MAIL_AUTOMATION_DENIED") }
        if number == -1744 { try fail("APPLE_MAIL_AUTOMATION_PERMISSION_REQUIRED") }
        if number == -600 { try fail("APPLE_MAIL_NOT_RUNNING") }
        if number == -1712 { try fail("APPLE_MAIL_REQUEST_TIMEOUT") }
        try fail("APPLE_MAIL_REQUEST_FAILED")
    }
    return result
}

func mailIcon() throws -> [String: Any] {
    guard let app = mailAppURL(), let tiff = NSWorkspace.shared.icon(forFile: app.path).tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: tiff),
          let png = bitmap.representation(using: .png, properties: [:]) else {
        try fail("APPLE_MAIL_UNAVAILABLE")
    }
    return ["png_base64": png.base64EncodedString()]
}

func string(_ row: NSAppleEventDescriptor, _ index: Int) -> String {
    let value = row.atIndex(index)
    return value?.stringValue ?? (value?.int32Value).map(String.init) ?? ""
}

func appleScriptLiteral(_ value: String) -> String {
    let normalized = value.replacingOccurrences(of: "\r\n", with: "\n")
        .replacingOccurrences(of: "\r", with: "\n")
        .replacingOccurrences(of: "\u{2028}", with: "\n")
        .replacingOccurrences(of: "\u{2029}", with: "\n")
    var pieces: [String] = []
    var segment = ""
    func quoted(_ text: String) -> String {
        "\"" + text.replacingOccurrences(of: "\\", with: "\\\\")
                  .replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }
    for character in normalized {
        if character == "\n" || character == "\t" {
            pieces.append(quoted(segment))
            pieces.append(character == "\n" ? "linefeed" : "tab")
            segment = ""
        } else {
            segment.append(character)
        }
    }
    pieces.append(quoted(segment))
    return pieces.joined(separator: " & ")
}

func sendMail(_ request: [String: Any]) throws -> [String: Any] {
    guard let to = request["to"] as? [String], !to.isEmpty,
          let subject = request["subject"] as? String,
          let body = request["body"] as? String else { try fail("CONNECTOR_ARGUMENT_INVALID") }
    let cc = request["cc"] as? [String] ?? []
    let bcc = request["bcc"] as? [String] ?? []
    let disclosure = "このメールはAIエージェントによって送信されています。"
    var finalBody = body.trimmingCharacters(in: .whitespacesAndNewlines)
    if finalBody.hasSuffix(disclosure) {
        finalBody = String(finalBody.dropLast(disclosure.count)).trimmingCharacters(in: .whitespacesAndNewlines)
    }
    finalBody = finalBody.isEmpty ? disclosure : finalBody + "\n\n" + disclosure
    var recipientScripts = to.map { "make new to recipient at end of to recipients with properties {address:\(appleScriptLiteral($0))}" }
    recipientScripts += cc.map { "make new cc recipient at end of cc recipients with properties {address:\(appleScriptLiteral($0))}" }
    recipientScripts += bcc.map { "make new bcc recipient at end of bcc recipients with properties {address:\(appleScriptLiteral($0))}" }
    let recipients = recipientScripts.joined(separator: "\n")
    let source = """
    tell application id "com.apple.mail"
        set outgoingMessage to make new outgoing message with properties {subject:\(appleScriptLiteral(subject)), visible:false}
        set message signature of outgoingMessage to missing value
        set content of outgoingMessage to \(appleScriptLiteral(finalBody))
        tell outgoingMessage
    \(recipients)
        end tell
        send outgoingMessage
        return true
    end tell
    """
    let result = try executeScript(source)
    guard result.booleanValue else { try fail("APPLE_MAIL_SEND_FAILED") }
    return ["sent": true]
}

func dateString(_ descriptor: NSAppleEventDescriptor?) -> String {
    guard let date = descriptor?.dateValue else { return "" }
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: date)
}

func list(_ limit: Int) throws -> [String: Any] {
    guard (1...50).contains(limit) else { try fail("CONNECTOR_ARGUMENT_INVALID") }
    let source = """
    tell application id "com.apple.mail"
        set totalMessages to count of messages of inbox
        set resultRows to {}
        if totalMessages > 0 then
            set countToRead to totalMessages
            if countToRead > 50 then set countToRead to 50
            repeat with messageIndex from 1 to countToRead
                set mailMessage to message messageIndex of inbox
                set end of resultRows to {id of mailMessage as text, subject of mailMessage as text, sender of mailMessage as text, date received of mailMessage, read status of mailMessage}
            end repeat
        end if
        return resultRows
    end tell
    """
    let result = try executeScript(source)
    var messages: [[String: Any]] = []
    if result.numberOfItems > 0 {
        for index in 1...result.numberOfItems {
            guard let row = result.atIndex(index), row.numberOfItems >= 5 else { continue }
            let ident = string(row, 1)
            guard !ident.isEmpty else { continue }
            let date = dateString(row.atIndex(4))
            messages.append(["id": ident, "threadId": ident, "subject": string(row, 2),
                             "sender": string(row, 3), "dateReceived": date,
                             "read": row.atIndex(5)?.booleanValue ?? false])
        }
    }
    return ["messages": messages, "mailbox": "INBOX"]
}

func get(_ ident: String) throws -> [String: Any] {
    guard ident.range(of: #"^\d{1,20}$"#, options: .regularExpression) != nil else {
        try fail("CONNECTOR_ARGUMENT_INVALID")
    }
    let source = """
    tell application id "com.apple.mail"
        set messageIdentifier to \(ident)
        set matchingMessages to (every message of inbox whose id is messageIdentifier)
        if (count of matchingMessages) is 0 then error "APPLE_MAIL_MESSAGE_NOT_FOUND"
        set mailMessage to item 1 of matchingMessages
        set bodyText to content of mailMessage as text
        set bodyLength to count of bodyText
        set wasTruncated to false
        if bodyLength > 200000 then
            set bodyText to text 1 thru 200000 of bodyText
            set wasTruncated to true
        end if
        return {id of mailMessage as text, subject of mailMessage as text, sender of mailMessage as text, date received of mailMessage, read status of mailMessage, bodyText, wasTruncated}
    end tell
    """
    let result = try executeScript(source)
    guard result.numberOfItems >= 7 else { try fail("APPLE_MAIL_INVALID_RESPONSE") }
    let identValue = string(result, 1)
    guard !identValue.isEmpty else { try fail("APPLE_MAIL_INVALID_RESPONSE") }
    return ["id": identValue, "threadId": identValue, "subject": string(result, 2),
            "sender": string(result, 3), "dateReceived": dateString(result.atIndex(4)),
            "read": result.atIndex(5)?.booleanValue ?? false,
            "content": string(result, 6), "content_truncated": result.atIndex(7)?.booleanValue ?? false]
}

func run(_ request: [String: Any]) throws -> [String: Any] {
    switch request["operation"] as? String ?? "status" {
    case "status": return status()
    case "icon": return try mailIcon()
    case "connect":
        guard mailAppURL() != nil else { try fail("APPLE_MAIL_UNAVAILABLE") }
        try ensureMailRunning()
        return try requestMailAuthorization(available: true)
    case "list":
        let limit = request["limit"] as? Int ?? 50
        return try list(limit)
    case "get":
        guard let ident = request["id"] as? String else { try fail("CONNECTOR_ARGUMENT_INVALID") }
        return try get(ident)
    case "send": return try sendMail(request)
    default: try fail("CONNECTOR_ACTION_UNSUPPORTED")
    }
}

do {
    guard let line = readLine(), let data = line.data(using: .utf8),
          let request = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        try fail("APPLE_MAIL_INVALID_REQUEST")
    }
    output(try run(request))
} catch let error as Failure {
    output(["error": error.code])
} catch {
    output(["error": "APPLE_MAIL_REQUEST_FAILED"])
}
