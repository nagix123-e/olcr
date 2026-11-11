import AppKit

private final class FixtureAccessibleElement: NSAccessibilityElement {
    private let parentElement: Any
    private let role: NSAccessibility.Role
    private let labelProvider: () -> String
    private let frameProvider: () -> NSRect

    init(parent: Any, role: NSAccessibility.Role, label: @escaping () -> String, frame: @escaping () -> NSRect) {
        parentElement = parent
        self.role = role
        labelProvider = label
        frameProvider = frame
        super.init()
    }

    override func accessibilityParent() -> Any? { parentElement }
    override func accessibilityRole() -> NSAccessibility.Role { role }
    override func accessibilityLabel() -> String? { labelProvider() }
    override func accessibilityFrame() -> NSRect { frameProvider() }
    override func accessibilityActionNames() -> [NSAccessibility.Action] { [] }
    @objc(isAccessibilityEnabled) var axEnabled: Bool { true }
}

private final class TrackingFixtureView: NSView {
    private var timer: Timer?
    private var phase: CGFloat = 0
    private var started = false
    private let inputField = NSTextField(frame: NSRect(x: 126, y: 34, width: 360, height: 30))
    private lazy var buttonElement = FixtureAccessibleElement(
        parent: self,
        role: .button,
        label: { [weak self] in self?.started == true ? "Started — tracking verified" : "Start tracking demo" },
        // Deliberately omit AX geometry so native action must use fresh visual
        // grounding; the animated button's frame remains visible in pixels.
        frame: { .zero }
    )
    private lazy var resultElement = FixtureAccessibleElement(
        parent: self,
        role: .staticText,
        label: { [weak self] in self?.started == true ? "Started — tracking verified" : "Waiting for click" },
        frame: { [weak self] in self?.screenFrame(NSRect(x: 34, y: 264, width: 520, height: 30)) ?? .zero }
    )

    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        inputField.identifier = NSUserInterfaceItemIdentifier("e2e-note")
        inputField.placeholderString = "Enter a test note"
        inputField.setAccessibilityLabel("E2E note")
        inputField.isEditable = true
        inputField.isSelectable = true
        inputField.isBezeled = true
        inputField.drawsBackground = true
        addSubview(inputField)
        timer = Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { [weak self] _ in
            guard let self, !self.started else { return }
            self.phase += 0.012
            self.needsDisplay = true
        }
    }

    required init?(coder: NSCoder) { nil }

    deinit { timer?.invalidate() }

    override func accessibilityChildren() -> [Any]? {
        [buttonElement, inputField, resultElement]
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor(calibratedRed: 0.075, green: 0.09, blue: 0.13, alpha: 1).setFill()
        bounds.fill()

        let buttonRect = currentButtonRect
        NSColor(calibratedRed: 0.16, green: 0.72, blue: 0.96, alpha: 1).setFill()
        NSBezierPath(roundedRect: buttonRect, xRadius: 13, yRadius: 13).fill()
        NSColor.white.setStroke()
        let mark = NSBezierPath()
        mark.lineWidth = 2
        for index in 0..<7 {
            let x = buttonRect.minX + 14 + CGFloat(index) * 11
            mark.move(to: NSPoint(x: x, y: buttonRect.minY + 11))
            mark.line(to: NSPoint(x: x, y: buttonRect.maxY - 11))
        }
        mark.stroke()

        let title = started ? "Started — tracking verified" : "Start tracking demo"
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 16, weight: .semibold),
            .foregroundColor: NSColor.white,
        ]
        let text = NSAttributedString(string: title, attributes: attributes)
        // Keep the label clear of the high-texture tracking marks so OCR can
        // ground its exact button label before the native tracker follows it.
        text.draw(at: NSPoint(x: buttonRect.minX + 100, y: buttonRect.midY - 9))
        if !started {
            "Button drifts slowly while capture is running".draw(
                at: NSPoint(x: 34, y: bounds.maxY - 36),
                withAttributes: [.font: NSFont.systemFont(ofSize: 13), .foregroundColor: NSColor.white]
            )
        }
        (started ? "Started — tracking verified" : "Waiting for click").draw(
            at: NSPoint(x: 34, y: 272),
            withAttributes: [.font: NSFont.systemFont(ofSize: 14, weight: .medium), .foregroundColor: NSColor.white]
        )
        "E2E note".draw(
            at: NSPoint(x: 34, y: 42),
            withAttributes: [.font: NSFont.systemFont(ofSize: 13), .foregroundColor: NSColor.white]
        )
    }

    override func mouseUp(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        fputs("FIXTURE_MOUSE_UP local=(\(Int(point.x)),\(Int(point.y))) button=(\(Int(currentButtonRect.minX)),\(Int(currentButtonRect.minY)),\(Int(currentButtonRect.width)),\(Int(currentButtonRect.height)))\n", stderr)
        guard currentButtonRect.contains(point), !started else { return }
        started = true
        needsDisplay = true
        NSAccessibility.post(element: self, notification: .layoutChanged)
    }

    private var currentButtonRect: NSRect {
        let travel = max(0, bounds.width - 360)
        let x = 34 + (travel == 0 ? 0 : (0.5 + 0.5 * sin(phase)) * travel)
        return NSRect(x: x, y: 112, width: 326, height: 68)
    }

    private func screenFrame(_ rect: NSRect) -> NSRect {
        guard let window else { return .zero }
        return window.convertToScreen(convert(rect, to: nil))
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit OLCR OpenCV Tracking Fixture", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu
        NSApp.mainMenu = mainMenu

        let view = TrackingFixtureView(frame: NSRect(x: 0, y: 0, width: 720, height: 380))
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 380),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "OLCR OpenCV Tracking Fixture"
        window.center()
        window.contentView = view
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let application = NSApplication.shared
application.setActivationPolicy(.regular)
private let delegate = AppDelegate()
application.delegate = delegate
application.run()
