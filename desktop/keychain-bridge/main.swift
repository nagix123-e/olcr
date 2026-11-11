import Foundation
import Security
import Darwin

private let service = "local.olcr.desktop.credential-bundle"
private let account = "all-providers-v1"
private let migrationMarker = "__olcr_legacy_migration_v1"
private let maximumBundleBytes = 256 * 1024

private func fail(_ code: String, _ status: OSStatus? = nil) -> Never {
    if let status {
        fputs("KEYCHAIN_BRIDGE_ERROR=\(code) status=\(status)\n", stderr)
    } else {
        fputs("KEYCHAIN_BRIDGE_ERROR=\(code)\n", stderr)
    }
    exit(status == errSecInteractionNotAllowed || status == errSecInteractionRequired || status == errSecAuthFailed ? 2 : 1)
}

private func readGenericPassword(keychain: SecKeychain, service: String, account: String)
    -> (OSStatus, Data?, SecKeychainItem?) {
    var length: UInt32 = 0
    var bytes: UnsafeMutableRawPointer?
    var item: SecKeychainItem?
    let status = service.withCString { serviceText in
        account.withCString { accountText in
            SecKeychainFindGenericPassword(keychain, UInt32(service.utf8.count), serviceText,
                UInt32(account.utf8.count), accountText, &length, &bytes, &item)
        }
    }
    guard status == errSecSuccess, let bytes else { return (status, nil, item) }
    defer { SecKeychainItemFreeContent(nil, bytes) }
    return (status, Data(bytes: bytes, count: Int(length)), item)
}

private func writeGenericPassword(keychain: SecKeychain, service: String, account: String,
                                  value: Data) -> OSStatus {
    let (lookup, _, item) = readGenericPassword(keychain: keychain, service: service, account: account)
    if lookup == errSecSuccess, let item {
        return value.withUnsafeBytes { bytes in
            SecKeychainItemModifyAttributesAndData(item, nil, UInt32(value.count), bytes.baseAddress)
        }
    }
    guard lookup == errSecItemNotFound else { return lookup }
    let added = value.withUnsafeBytes { bytes in
        guard let baseAddress = bytes.baseAddress else { return errSecParam }
        return service.withCString { serviceText in
            return account.withCString { accountText in
                SecKeychainAddGenericPassword(keychain, UInt32(service.utf8.count), serviceText,
                    UInt32(account.utf8.count), accountText, UInt32(value.count), baseAddress, nil)
            }
        }
    }
    if added != errSecDuplicateItem { return added }
    let (racedLookup, _, racedItem) = readGenericPassword(keychain: keychain, service: service, account: account)
    guard racedLookup == errSecSuccess, let racedItem else { return racedLookup }
    return value.withUnsafeBytes { bytes in
        SecKeychainItemModifyAttributesAndData(racedItem, nil, UInt32(value.count), bytes.baseAddress)
    }
}

// Automatic restoration must never put a password dialog over navigation.
// Only an explicit user action may enable Keychain interaction.
if CommandLine.arguments == [CommandLine.arguments[0], "--protocol-version"] {
    print("2")
    exit(0)
}
let arguments = Array(CommandLine.arguments.dropFirst())
let allowlistArguments = arguments.filter { $0.hasPrefix("--allowlist-json=") }
guard allowlistArguments.count == 1,
      arguments.filter({ $0.hasPrefix("--interaction=") }).count <= 1,
      arguments.filter({ $0 == "--update-stdin" }).count <= 1,
      arguments.allSatisfy({ $0.hasPrefix("--allowlist-json=") || $0 == "--interaction=allow" || $0 == "--interaction=forbid" || $0 == "--update-stdin" }) else {
    fail("invalid_arguments")
}
let interactive = arguments.contains("--interaction=allow")
SecKeychainSetUserInteractionAllowed(interactive)
let allowlistText = String(allowlistArguments[0].dropFirst("--allowlist-json=".count))
guard let allowlistData = allowlistText.data(using: .utf8),
      let allowlist = try? JSONDecoder().decode([String].self, from: allowlistData),
      !allowlist.isEmpty,
      allowlist.count <= 128,
      allowlist.allSatisfy({ !$0.isEmpty && $0.utf8.count <= 128 && $0 != migrationMarker }) else {
    fail("invalid_allowlist")
}
let allowed = Set(allowlist)
struct BundleUpdate: Decodable { let key: String; let value: String? }
var update: BundleUpdate?
if arguments.contains("--update-stdin") {
    // Secrets travel over an anonymous pipe, never command arguments or files.
    let input = FileHandle.standardInput.readData(ofLength: maximumBundleBytes + 1)
    guard input.count <= maximumBundleBytes,
          let parsed = try? JSONDecoder().decode(BundleUpdate.self, from: input),
          allowed.contains(parsed.key),
          parsed.value == nil || (!parsed.value!.isEmpty && parsed.value!.utf8.count <= 16 * 1024) else {
        fail("invalid_update")
    }
    update = parsed
}
// Serialize the complete read/merge/write across helpers and app versions.
#if KEYCHAIN_BRIDGE_TEST
let lockDirectory = URL(fileURLWithPath: ProcessInfo.processInfo.environment["OLCR_TEST_KEYCHAIN"]!).deletingLastPathComponent()
#else
let lockDirectory = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/OLCR/KeychainBridge", isDirectory: true)
#endif
do { try FileManager.default.createDirectory(at: lockDirectory, withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700]) } catch { fail("lock_unavailable") }
let lockFD = open(lockDirectory.appendingPathComponent("credentials.lock").path,
                  O_CREAT | O_RDWR | O_NOFOLLOW, S_IRUSR | S_IWUSR)
guard lockFD >= 0, flock(lockFD, LOCK_EX) == 0 else { fail("lock_unavailable") }
defer { flock(lockFD, LOCK_UN); close(lockFD) }

var keychain: SecKeychain?
#if KEYCHAIN_BRIDGE_TEST
let defaultKeychainStatus = SecKeychainOpen(ProcessInfo.processInfo.environment["OLCR_TEST_KEYCHAIN"]!, &keychain)
#else
let defaultKeychainStatus = SecKeychainCopyDefault(&keychain)
#endif
guard defaultKeychainStatus == errSecSuccess, let keychain else {
    fail("default_keychain_unavailable", defaultKeychainStatus)
}

let (bundleStatus, bundleData, _) = readGenericPassword(keychain: keychain, service: service, account: account)
var storedBundle: [String: String]
var legacyMigrated = false
if bundleStatus == errSecSuccess, let bundleData {
    guard bundleData.count <= maximumBundleBytes else { fail("credential_bundle_too_large") }
    guard let parsed = try? JSONDecoder().decode([String: String].self, from: bundleData) else {
        fail("credential_bundle_invalid")
    }
    storedBundle = parsed
} else if bundleStatus == errSecItemNotFound {
    storedBundle = [:]
} else {
    fail("credential_bundle_unavailable", bundleStatus)
}

if storedBundle[migrationMarker] != "true" {
    // One-time merge of OLCR's previous per-provider items. Read only allowlisted
    // accounts absent from the bundle; retain old items as recovery copies.
    var migrated = storedBundle
    for key in allowlist {
        if migrated[key] != nil { continue }
        let legacyService: String
        let legacyAccount: String
        if key == "web:brave" || key == "web:tavily" {
            legacyService = "local.olcr.desktop.web-search"
            legacyAccount = String(key.dropFirst("web:".count))
        } else {
            legacyService = "local.olcr.desktop.external-api"
            legacyAccount = key
        }
        let (status, value, _) = readGenericPassword(keychain: keychain, service: legacyService,
                                                     account: legacyAccount)
        if status == errSecItemNotFound { continue }
        guard status == errSecSuccess, let value,
              value.count <= 16 * 1024, let text = String(data: value, encoding: .utf8),
              !text.isEmpty else {
            fail("legacy_credential_read_failed", status)
        }
        migrated[key] = text
    }
    migrated[migrationMarker] = "true"
    guard let data = try? JSONEncoder().encode(migrated), data.count <= maximumBundleBytes else {
        fail("credential_bundle_output_invalid")
    }
    let status = writeGenericPassword(keychain: keychain, service: service, account: account, value: data)
    guard status == errSecSuccess else { fail("credential_bundle_migration_failed", status) }
    storedBundle = migrated
    legacyMigrated = true
}

if let update {
    storedBundle[update.key] = update.value
    guard let data = try? JSONEncoder().encode(storedBundle), data.count <= maximumBundleBytes else {
        fail("credential_bundle_output_invalid")
    }
    let status = writeGenericPassword(keychain: keychain, service: service, account: account, value: data)
    guard status == errSecSuccess else { fail("credential_bundle_store_failed", status) }
}

let filteredBundle = storedBundle.filter { key, value in
    allowed.contains(key) && !value.isEmpty && value.utf8.count <= 16 * 1024
}
guard let output = try? JSONEncoder().encode(filteredBundle), output.count <= maximumBundleBytes else {
    fail("credential_bundle_output_invalid")
}

FileHandle.standardOutput.write(output)
FileHandle.standardOutput.write(Data([0x0A]))
fputs("KEYCHAIN_BRIDGE_RESTORE_COMPLETE credentials=\(filteredBundle.count)\n", stderr)
fputs("KEYCHAIN_BRIDGE_LEGACY_BUNDLE_MIGRATED=\(legacyMigrated)\n", stderr)
