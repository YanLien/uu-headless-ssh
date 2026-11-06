import Foundation
import CryptoKit
import IOKit
import Darwin

// Reuse this Mac's existing UU login in memory. Credentials are never printed
// or written to a second configuration file.
enum LaunchError: Error {
    case missingPreference, invalidEnvelope, machineIdentity, decryption, invalidLogin
}

func machineIdentity() throws -> String {
    let service = IOServiceGetMatchingService(kIOMainPortDefault,
                                              IOServiceMatching("IOPlatformExpertDevice"))
    guard service != 0 else { throw LaunchError.machineIdentity }
    defer { IOObjectRelease(service) }
    guard let value = IORegistryEntryCreateCFProperty(
        service, "IOPlatformUUID" as CFString, kCFAllocatorDefault, 0
    )?.takeRetainedValue() as? String else { throw LaunchError.machineIdentity }
    return value
}

func openPreference(_ value: String, machine: String) throws -> String {
    guard value.hasPrefix("__nrd__"),
          let encoded = Data(base64Encoded: String(value.dropFirst(7))),
          let envelope = try JSONSerialization.jsonObject(with: encoded) as? [String: String],
          let ctValue = envelope["ct"], let ct = Data(base64Encoded: ctValue),
          let nonceValue = envelope["nonce"], let nonce = Data(base64Encoded: nonceValue)
    else { throw LaunchError.invalidEnvelope }

    let salt = Data("f7Kx2mQpL9vRnT4wYjBc8eAsDhWuZ3Ng".utf8)
    let info = Data("Vc5tXqP0mJbRkHyE2sLwN6oAiGzFdU8T".utf8)
    // The installed app derives its key from the hardware UUID followed by
    // its bundle identifier, with HKDF-SHA256 and a 32-byte output.
    let key = HKDF<SHA256>.deriveKey(
        inputKeyMaterial: SymmetricKey(data: Data((machine + "com.netease.uuremote").utf8)),
        salt: salt, info: info, outputByteCount: 32
    )
    if let box = try? AES.GCM.SealedBox(combined: ct),
       let data = try? AES.GCM.open(box, using: key),
       let result = String(data: data, encoding: .utf8) { return result }

    if ct.count >= 16,
       let box = try? AES.GCM.SealedBox(nonce: AES.GCM.Nonce(data: nonce),
                                       ciphertext: ct.dropLast(16), tag: ct.suffix(16)),
       let data = try? AES.GCM.open(box, using: key),
       let result = String(data: data, encoding: .utf8) { return result }

    throw LaunchError.decryption
}

func base64URL(_ value: String) -> Data? {
    var encoded = value.replacingOccurrences(of: "-", with: "+")
        .replacingOccurrences(of: "_", with: "/")
    encoded += String(repeating: "=", count: (4 - encoded.count % 4) % 4)
    return Data(base64Encoded: encoded)
}

do {
    let plistURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Preferences/com.netease.uuremote.plist")
    let raw = try Data(contentsOf: plistURL)
    guard let preferences = try PropertyListSerialization.propertyList(
        from: raw, options: [], format: nil
    ) as? [String: Any] else { throw LaunchError.missingPreference }
    let machine = try machineIdentity()
    func preference(_ name: String) throws -> String {
        guard let value = preferences[name] as? String else { throw LaunchError.missingPreference }
        let raw = try openPreference(value, machine: machine)
        if let data = raw.data(using: .utf8),
           let string = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) as? String {
            return string
        }
        return raw
    }

    let user = try preference("NTES_NRD__MACuserID")
    let device = try preference("NTES_NRD__MACdeviceID")
    let token = try preference("NTES_NRD__MACtoken")
    let parts = token.split(separator: ".")
    guard parts.count == 3, let payload = base64URL(String(parts[1])),
          let claims = try JSONSerialization.jsonObject(with: payload) as? [String: Any],
          let csrf = claims["csrf"] as? String, !csrf.isEmpty
    else { throw LaunchError.invalidLogin }

    if CommandLine.arguments.dropFirst().first == "--inspect" {
        print("Existing UU login is readable on this Mac; user/device/token/CSRF present.")
        exit(0)
    }
    guard CommandLine.arguments.count >= 2 else {
        print("Usage: uu-headless-launch CLIENT [ARGS...]")
        exit(2)
    }
    var environment = ProcessInfo.processInfo.environment
    environment["UU_USER_ID"] = (claims["sub"] as? String) ?? user
    environment["UU_DEVICE_ID"] = (claims["device_id"] as? String) ?? device
    environment["UU_TOKEN"] = token
    environment["UU_CSRF"] = csrf
    guard let client = claims["client_id"] as? String,
          let system = claims["system_id"] as? String,
          !client.isEmpty else { throw LaunchError.invalidLogin }
    environment["UU_CLIENT_ID"] = client
    environment["UU_SYSTEM_ID"] = system
    environment["UU_LOGIN_WINDOW"] = "0"
    environment["RUST_LOG"] = "warn"
    // All login fields come from memory. Avoid merging an unrelated account
    // from the protocol client's normal on-disk configuration file.
    environment["UU_CONFIG"] = FileManager.default.temporaryDirectory
        .appendingPathComponent(UUID().uuidString + "-uu-unused-config.json").path
    // Keep the controlling TTY and foreground process group. Foundation.Process
    // creates a new group, which suspends an interactive terminal on tcsetattr.
    for (key, value) in environment { setenv(key, value, 1) }
    let executable = CommandLine.arguments[1]
    var arguments: [UnsafeMutablePointer<CChar>?] = CommandLine.arguments.dropFirst().map { strdup($0) }
    arguments.append(nil)
    arguments.withUnsafeMutableBufferPointer { buffer in
        _ = execv(executable, buffer.baseAddress!)
    }
    fputs("Could not start the UU text client. No credentials displayed.\n", stderr)
    exit(1)
} catch let error as LaunchError {
    fputs("Cannot reuse the existing UU login: \(error). No credentials displayed.\n", stderr)
    exit(1)
} catch {
    fputs("UU login preparation failed. No credentials displayed.\n", stderr)
    exit(1)
}
