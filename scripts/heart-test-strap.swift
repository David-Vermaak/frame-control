// A synthetic Bluetooth heart-rate strap for testing, run on the Mac.
//
//   swiftc -O scripts/heart-test-strap.swift -o /tmp/heart-test-strap
//   /tmp/heart-test-strap [seconds]      # default 60
//
// Advertises the standard Heart Rate Service (180d) as "FC Test Strap" and,
// while a central is subscribed, sends one Heart Rate Measurement (2a37) per
// second from a fixed, known sequence. Each sent value is printed to stdout as
// `unix_seconds,bpm,flags` so scripts/heart-check.py can compare what the
// Frame received with what was sent. Every value is synthetic; this is not a
// sensor. macOS asks for Bluetooth permission the first time it runs.
import CoreBluetooth
import Foundation

let hrs = CBUUID(string: "180D")
let measurement = CBUUID(string: "2A37")

/// The known sequence: an 8-bit ramp, 16-bit encodings, a skin-contact loss
/// (which must show as no reading) and a recovery.
func packet(_ second: Int) -> (bpm: Int, flags: UInt8) {
    switch second % 60 {
    case 0..<30: return (60 + second % 60 * 4, 0x06)   // 60…176, contact detected
    case 30..<40: return (180 - (second % 60 - 30) * 3, 0x07) // 16-bit value
    case 40..<45: return (150, 0x04)                  // contact lost
    default: return (72 + (second % 60 - 45), 0x06)
    }
}

final class Strap: NSObject, CBPeripheralManagerDelegate {
    let seconds: Int
    var manager: CBPeripheralManager!
    var characteristic: CBMutableCharacteristic!
    var subscribed = false
    var sent = 0

    init(seconds: Int) {
        self.seconds = seconds
        super.init()
        manager = CBPeripheralManager(delegate: self, queue: nil)
    }

    func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        guard peripheral.state == .poweredOn else {
            FileHandle.standardError.write("Bluetooth state \(peripheral.state.rawValue)\n".data(using: .utf8)!)
            if peripheral.state == .unauthorized || peripheral.state == .unsupported || peripheral.state == .poweredOff {
                FileHandle.standardError.write("Bluetooth unavailable (state \(peripheral.state.rawValue))\n".data(using: .utf8)!)
                exit(1)
            }
            return
        }
        characteristic = CBMutableCharacteristic(type: measurement, properties: [.notify], value: nil, permissions: [])
        let service = CBMutableService(type: hrs, primary: true)
        service.characteristics = [characteristic]
        peripheral.add(service)
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didAdd service: CBService, error: Error?) {
        if let error { fail("add service: \(error.localizedDescription)") }
        peripheral.startAdvertising([CBAdvertisementDataLocalNameKey: "FC Test Strap",
                                     CBAdvertisementDataServiceUUIDsKey: [hrs]])
    }

    func peripheralManagerDidStartAdvertising(_ peripheral: CBPeripheralManager, error: Error?) {
        if let error { fail("advertise: \(error.localizedDescription)") }
        FileHandle.standardError.write("Advertising FC Test Strap\n".data(using: .utf8)!)
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in self.tick() }
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral, didSubscribeTo characteristic: CBCharacteristic) {
        subscribed = true
        FileHandle.standardError.write("Central subscribed\n".data(using: .utf8)!)
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral, didUnsubscribeFrom characteristic: CBCharacteristic) {
        subscribed = false
        FileHandle.standardError.write("Central unsubscribed\n".data(using: .utf8)!)
    }

    func tick() {
        guard subscribed else { return }
        if sent >= seconds { exit(0) }
        let (bpm, flags) = packet(sent)
        var bytes: [UInt8] = [flags, UInt8(bpm & 0xff)]
        if flags & 1 != 0 { bytes.append(UInt8(bpm >> 8)) }
        if manager.updateValue(Data(bytes), for: characteristic, onSubscribedCentrals: nil) {
            print(String(format: "%.3f,%d,%d", Date().timeIntervalSince1970, bpm, flags))
            fflush(stdout)
            sent += 1
        }
    }

    func fail(_ message: String) -> Never {
        FileHandle.standardError.write("\(message)\n".data(using: .utf8)!)
        exit(1)
    }
}

let seconds = CommandLine.arguments.count > 1 ? Int(CommandLine.arguments[1]) ?? 60 : 60
let strap = Strap(seconds: seconds)
RunLoop.main.run()
