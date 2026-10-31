import AppKit

let image = NSImage(size: NSSize(width: 1024, height: 1024))
image.lockFocus()
let background = NSBezierPath(roundedRect: NSRect(x: 42, y: 42, width: 940, height: 940), xRadius: 210, yRadius: 210)
NSGradient(starting: NSColor(red: 0.05, green: 0.13, blue: 0.19, alpha: 1),
           ending: NSColor(red: 0.09, green: 0.35, blue: 0.34, alpha: 1))!.draw(in: background, angle: 55)
NSColor(white: 0.94, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 300, y: 165, width: 424, height: 700), xRadius: 78, yRadius: 78).fill()
NSColor(red: 0.08, green: 0.17, blue: 0.22, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 321, y: 188, width: 382, height: 655), xRadius: 57, yRadius: 57).fill()
let colors: [NSColor] = [.systemTeal, .systemOrange, .systemBlue, .systemPurple, .systemGreen, .systemPink, .systemYellow, .systemIndigo, .systemCyan]
for row in 0..<3 {
    for column in 0..<3 {
        colors[row * 3 + column].setFill()
        NSBezierPath(roundedRect: NSRect(x: 353 + column * 108, y: 580 - row * 112, width: 88, height: 88), xRadius: 22, yRadius: 22).fill()
    }
}
NSColor(white: 0.9, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 435, y: 213, width: 154, height: 10), xRadius: 5, yRadius: 5).fill()
NSColor(red: 0.08, green: 0.17, blue: 0.22, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 448, y: 784, width: 128, height: 30), xRadius: 15, yRadius: 15).fill()
image.unlockFocus()
let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
