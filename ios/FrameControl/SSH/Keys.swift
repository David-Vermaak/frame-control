import CryptoKit
import Foundation
import NIOSSH
import Security

/// Small wrapper over the Keychain for this app's secrets.
enum Keychain {
    private static let service = "com.saphid.framecontrol"

    private static func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    static func data(_ account: String) -> Data? {
        var q = query(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        return SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess ? out as? Data : nil
    }

    static func set(_ data: Data, _ account: String) {
        SecItemDelete(query(account) as CFDictionary)
        var q = query(account)
        q[kSecValueData as String] = data
        // Only on this device and not in backups: the key is this phone's identity.
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(q as CFDictionary, nil)
    }

    static func delete(_ account: String) {
        SecItemDelete(query(account) as CFDictionary)
    }
}

/// This phone's SSH key: ed25519, made once, kept in the Keychain.
enum DeviceKey {
    private static let account = "ssh-ed25519"

    static func loadOrCreate() -> Curve25519.Signing.PrivateKey {
        if let raw = Keychain.data(account), let key = try? Curve25519.Signing.PrivateKey(rawRepresentation: raw) {
            return key
        }
        let key = Curve25519.Signing.PrivateKey()
        Keychain.set(key.rawRepresentation, account)
        return key
    }

    /// The line for ~/.ssh/authorized_keys, e.g. "ssh-ed25519 AAAA… frame-control@iPhone".
    static func authorizedKeysLine(_ key: Curve25519.Signing.PrivateKey, comment: String) -> String {
        String(openSSHPublicKey: NIOSSHPrivateKey(ed25519Key: key).publicKey) + " " + comment
    }
}
