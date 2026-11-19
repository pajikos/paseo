import CoreGraphics
import Foundation

let pid = Int(CommandLine.arguments[1])!
let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
let windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
for window in windows {
    guard window[kCGWindowOwnerPID as String] as? Int == pid,
          window[kCGWindowLayer as String] as? Int == 0,
          let alpha = window[kCGWindowAlpha as String] as? Double, alpha > 0,
          let bounds = window[kCGWindowBounds as String] as? [String: Double],
          let width = bounds["Width"], let height = bounds["Height"],
          width >= 100, height >= 100 else { continue }
    print("WindowServer shows a \(Int(width)) x \(Int(height)) window owned by Paseo PID \(pid).")
    exit(0)
}
exit(1)
