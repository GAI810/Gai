import AppKit

final class HUDController: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var window: NSWindow!
    var blur: NSVisualEffectView!
    var dot: PulseDot!
    var iconView: NSImageView!
    var statusLabel: NSTextField!
    var countLabel: NSTextField!
    var detailLabel: NSTextField!
    var badgeLabel: NSTextField!

    var timer: Timer?
    var snapTimer: Timer?
    var suppressSnapUntil = Date.distantPast
    var collapsed = false
    var collapsedSide = "right"
    var iconStyle = UserDefaults.standard.string(forKey: "iconStyle") ?? "tray.and.arrow.down.fill"

    let expandedSize = NSSize(width: 232, height: 96)
    let collapsedSize = NSSize(width: 60, height: 60)

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        restorePosition()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        // 每秒检查：收起状态被拖离边缘时立即展开（不依赖拖动事件）
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self = self, self.collapsed,
                  let screen = self.window.screen ?? NSScreen.main else { return }
            let vf = screen.visibleFrame
            let f = self.window.frame
            let nearEdge = f.minX <= vf.minX + 36 || f.maxX >= vf.maxX - 36
            if !nearEdge {
                self.applyExpanded(animated: true, keepOrigin: true)
            }
        }
        // 自测钩子（仅当环境变量存在时生效，不影响正常使用）
        if ProcessInfo.processInfo.environment["HUD_TEST_EXPAND"] != nil {
            Timer.scheduledTimer(withTimeInterval: 3, repeats: false) { [weak self] _ in
                self?.applyExpanded(animated: true, keepOrigin: true)
            }
        }
    }

    func buildWindow() {
        let rect = NSRect(origin: .zero, size: expandedSize)
        window = NSWindow(contentRect: rect,
                          styleMask: [.borderless, .nonactivatingPanel],
                          backing: .buffered, defer: false)
        window.level = .screenSaver
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.isMovableByWindowBackground = true
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        window.delegate = self
        window.contentView?.wantsLayer = true
        window.contentView?.layer?.masksToBounds = true

        blur = NSVisualEffectView(frame: rect)
        blur.material = .hudWindow
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.wantsLayer = true
        blur.layer?.cornerRadius = 16
        blur.layer?.masksToBounds = true
        blur.autoresizingMask = [.width, .height]

        iconView = NSImageView(frame: NSRect(x: 16, y: 64, width: 20, height: 20))
        iconView.image = NSImage(systemSymbolName: iconStyle, accessibilityDescription: nil)
        iconView.contentTintColor = .white
        iconView.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 16, weight: .medium)

        dot = PulseDot(frame: NSRect(x: 42, y: 68, width: 10, height: 10))

        statusLabel = label(NSRect(x: 58, y: 62, width: 160, height: 20),
                            size: 13, weight: .medium, color: .white)
        countLabel = label(NSRect(x: 16, y: 30, width: 200, height: 32),
                           size: 26, weight: .semibold, color: .white)
        detailLabel = label(NSRect(x: 16, y: 10, width: 200, height: 18),
                            size: 11, weight: .regular, color: .secondaryLabelColor)
        badgeLabel = label(NSRect(x: 34, y: 2, width: 26, height: 16),
                           size: 11, weight: .semibold, color: .white)
        badgeLabel.alignment = .center
        badgeLabel.isHidden = true

        blur.addSubview(iconView)
        blur.addSubview(dot)
        blur.addSubview(statusLabel)
        blur.addSubview(countLabel)
        blur.addSubview(detailLabel)
        blur.addSubview(badgeLabel)
        window.contentView = blur

        let click = NSClickGestureRecognizer(target: self, action: #selector(handleClick))
        blur.addGestureRecognizer(click)

        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "打开状态面板", action: #selector(openPanel), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "打开入库记录", action: #selector(openReport), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "收起贴左", action: #selector(snapLeft), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "收起贴右", action: #selector(snapRight), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "展开", action: #selector(expandAction), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        let iconMenu = NSMenuItem(title: "图标样式", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        for (title, symbol) in [("下载进库", "tray.and.arrow.down.fill"),
                                 ("文档入库", "arrow.down.doc.fill"),
                                 ("收件箱", "tray.full.fill"),
                                 ("心跳", "heart.fill")] {
            let item = NSMenuItem(title: title, action: #selector(changeIcon(_:)), keyEquivalent: "")
            item.representedObject = symbol
            sub.addItem(item)
        }
        iconMenu.submenu = sub
        menu.addItem(iconMenu)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "退出悬浮窗", action: #selector(quit), keyEquivalent: ""))
        blur.menu = menu

        window.orderFrontRegardless()
    }

    func label(_ frame: NSRect, size: CGFloat, weight: NSFont.Weight, color: NSColor) -> NSTextField {
        let f = NSTextField(frame: frame)
        f.isEditable = false
        f.isBordered = false
        f.drawsBackground = false
        f.font = NSFont.systemFont(ofSize: size, weight: weight)
        f.textColor = color
        return f
    }

    // MARK: - 位置 / 吸附 / 收起

    func restorePosition() {
        let defaults = UserDefaults.standard
        if defaults.bool(forKey: "isCollapsed") {
            collapsedSide = defaults.string(forKey: "collapsedSide") ?? "right"
            applyCollapsed(side: collapsedSide, animated: false)
            return
        }
        if defaults.object(forKey: "hudX") != nil {
            var origin = NSPoint(x: defaults.double(forKey: "hudX"), y: defaults.double(forKey: "hudY"))
            if let screen = NSScreen.main {
                let vf = screen.visibleFrame
                origin.x = min(max(origin.x, vf.minX + 60), vf.maxX - expandedSize.width - 60)
                origin.y = min(max(origin.y, vf.minY), vf.maxY - expandedSize.height)
            }
            suppressSnapUntil = Date().addingTimeInterval(1.5)
            window.setFrameOrigin(origin)
        } else if let screen = NSScreen.main {
            let vf = screen.visibleFrame
            window.setFrameOrigin(NSPoint(x: vf.maxX - expandedSize.width - 24, y: vf.minY + 24))
        }
    }

    func windowDidMove(_ notification: Notification) {
        snapTimer?.invalidate()
        snapTimer = Timer.scheduledTimer(withTimeInterval: 0.35, repeats: false) { [weak self] _ in
            self?.snapIfNeeded()
        }
    }

    func snapIfNeeded() {
        if Date() < suppressSnapUntil { return }
        guard let screen = window.screen ?? NSScreen.main else { return }
        let vf = screen.visibleFrame
        let f = window.frame
        if f.minX <= vf.minX + 36 {
            applyCollapsed(side: "left", animated: true)
        } else if f.maxX >= vf.maxX - 36 {
            applyCollapsed(side: "right", animated: true)
        } else if collapsed {
            // 从边缘拖出来 -> 恢复完整卡片，并停在拖到的位置
            applyExpanded(animated: true, keepOrigin: true)
        } else {
            savePosition()
        }
    }

    func applyCollapsed(side: String, animated: Bool) {
        collapsed = true
        collapsedSide = side
        UserDefaults.standard.set(true, forKey: "isCollapsed")
        UserDefaults.standard.set(side, forKey: "collapsedSide")

        statusLabel.isHidden = true
        countLabel.isHidden = true
        detailLabel.isHidden = true
        badgeLabel.isHidden = false

        guard let screen = window.screen ?? NSScreen.main else { return }
        let vf = screen.visibleFrame
        let size = collapsedSize
        let x = side == "left" ? vf.minX + 2 : vf.maxX - size.width - 2
        let y = vf.midY - size.height / 2

        blur.layer?.cornerRadius = 18
        blur.frame = NSRect(origin: .zero, size: size)
        iconView.frame = NSRect(x: (size.width - 26) / 2, y: (size.height - 26) / 2 + 2, width: 26, height: 26)
        dot.frame = NSRect(x: size.width - 16, y: size.height - 16, width: 9, height: 9)
        badgeLabel.frame = NSRect(x: 6, y: 5, width: size.width - 12, height: 14)

        suppressSnapUntil = Date().addingTimeInterval(0.6)
        let frame = NSRect(x: x, y: y, width: size.width, height: size.height)
        if animated {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.22
                window.animator().setFrame(frame, display: true)
            }
        } else {
            window.setFrame(frame, display: true)
        }
    }

    func applyExpanded(animated: Bool, keepOrigin: Bool = false) {
        collapsed = false
        UserDefaults.standard.set(false, forKey: "isCollapsed")
        statusLabel.isHidden = false
        countLabel.isHidden = false
        detailLabel.isHidden = false
        badgeLabel.isHidden = true

        guard let screen = window.screen ?? NSScreen.main else { return }
        let vf = screen.visibleFrame
        let size = expandedSize
        blur.layer?.cornerRadius = 16
        blur.frame = NSRect(origin: .zero, size: size)
        iconView.frame = NSRect(x: 16, y: 64, width: 20, height: 20)
        dot.frame = NSRect(x: 42, y: 68, width: 10, height: 10)
        statusLabel.frame = NSRect(x: 58, y: 62, width: 160, height: 20)
        countLabel.frame = NSRect(x: 16, y: 30, width: 200, height: 32)
        detailLabel.frame = NSRect(x: 16, y: 10, width: 200, height: 18)

        var originX = window.frame.origin.x
        var originY = window.frame.origin.y
        if keepOrigin {
            originX = min(max(originX, vf.minX), vf.maxX - size.width)
            originY = min(max(originY, vf.minY), vf.maxY - size.height)
        } else {
            originX = collapsedSide == "left" ? vf.minX + 12 : vf.maxX - size.width - 12
            originY = min(max(window.frame.midY - size.height / 2, vf.minY), vf.maxY - size.height)
        }
        suppressSnapUntil = Date().addingTimeInterval(0.6)
        let frame = NSRect(x: originX, y: originY, width: size.width, height: size.height)
        if animated {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.22
                window.animator().setFrame(frame, display: true)
            }
        } else {
            window.setFrame(frame, display: true)
        }
    }

    func savePosition() {
        let defaults = UserDefaults.standard
        defaults.set(window.frame.origin.x, forKey: "hudX")
        defaults.set(window.frame.origin.y, forKey: "hudY")
    }

    @objc func handleClick() {
        if collapsed {
            applyExpanded(animated: true)
        } else {
            openPanel()
        }
    }

    @objc func snapLeft() { applyCollapsed(side: "left", animated: true) }
    @objc func snapRight() { applyCollapsed(side: "right", animated: true) }
    @objc func expandAction() { applyExpanded(animated: true) }

    @objc func openPanel() {
        if let url = URL(string: "http://127.0.0.1:8787") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func openReport() {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let candidates = [
            home.appendingPathComponent("Library/Application Support/ResumeHunter/app/data/reports"),
            home.appendingPathComponent(".claude/skills/resume-hunter/data/reports"),
            home.appendingPathComponent("简历一键入库/data/reports")
        ]
        for dir in candidates {
            if let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil),
               let report = files.first(where: { $0.lastPathComponent.hasSuffix(".html") }) {
                NSWorkspace.shared.open(report)
                return
            }
        }
        openPanel()
    }

    @objc func changeIcon(_ sender: NSMenuItem) {
        guard let symbol = sender.representedObject as? String else { return }
        iconStyle = symbol
        UserDefaults.standard.set(symbol, forKey: "iconStyle")
        iconView.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
    }

    @objc func quit() { NSApp.terminate(nil) }

    // MARK: - 数据刷新

    func statusToken() -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let paths = [
            home.appendingPathComponent("Library/Application Support/ResumeHunter/app/data/status_token"),
            home.appendingPathComponent(".claude/skills/resume-hunter/data/status_token"),
            home.appendingPathComponent("简历一键入库/data/status_token")
        ]
        for p in paths {
            if let t = try? String(contentsOf: p, encoding: .utf8) {
                return t.trimmingCharacters(in: .whitespacesAndNewlines)
            }
        }
        return ""
    }

    func refresh() {
        guard var comps = URLComponents(string: "http://127.0.0.1:8787/api/status") else { return }
        let token = statusToken()
        if !token.isEmpty { comps.queryItems = [URLQueryItem(name: "token", value: token)] }
        guard let url = comps.url else { return }
        var req = URLRequest(url: url)
        if !token.isEmpty { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self = self else { return }
            var ok = false
            var today = 0
            var age: Int? = nil
            var last = ""
            if let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                ok = (obj["watch"] as? [String: Any])?["ok"] as? Bool ?? false
                today = obj["today"] as? Int ?? 0
                age = (obj["watch"] as? [String: Any])?["age"] as? Int
                if let lastObj = obj["last"] as? [String: Any],
                   let name = lastObj["name"] as? String, !name.isEmpty {
                    let ts = (lastObj["ts"] as? String) ?? ""
                    last = "最近 " + (ts.count >= 16 ? String(ts.dropFirst(11).prefix(5)) : "") + " " + name
                }
            }
            DispatchQueue.main.async {
                if self.collapsed, let screen = self.window.screen ?? NSScreen.main {
                    let vf = screen.visibleFrame
                    let f = self.window.frame
                    let nearEdge = f.minX <= vf.minX + 36 || f.maxX >= vf.maxX - 36
                    if !nearEdge {
                        self.applyExpanded(animated: true, keepOrigin: true)
                    }
                }
                if !self.collapsed { self.savePosition() }
                let color = ok ? NSColor.systemGreen : NSColor.systemRed
                self.iconView.contentTintColor = color
                self.dot.setColor(color, pulsing: ok)
                self.statusLabel.stringValue = ok ? "简历入库 · 运行中" : "简历入库 · 已停止"
                self.countLabel.stringValue = "\(today) 份"
                self.badgeLabel.stringValue = "\(today)"
                var parts: [String] = []
                if let age = age { parts.append("心跳 \(age) 秒前") }
                if !last.isEmpty { parts.append(last) }
                self.detailLabel.stringValue = parts.isEmpty ? "等待数据…" : parts.joined(separator: " · ")
            }
        }.resume()
    }
}

final class PulseDot: NSView {
    private let core = CALayer()
    private let ring = CAShapeLayer()

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = .clear

        ring.path = CGPath(ellipseIn: bounds.insetBy(dx: 1, dy: 1), transform: nil)
        ring.fillColor = NSColor.clear.cgColor
        ring.strokeColor = NSColor.systemGreen.cgColor
        ring.lineWidth = 1.5
        ring.opacity = 0
        layer?.addSublayer(ring)

        core.frame = bounds.insetBy(dx: 2.5, dy: 2.5)
        core.cornerRadius = core.frame.width / 2
        core.backgroundColor = NSColor.systemGreen.cgColor
        layer?.addSublayer(core)
    }

    required init?(coder: NSCoder) { fatalError() }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        layoutLayers()
    }

    override func setFrameOrigin(_ newOrigin: NSPoint) {
        super.setFrameOrigin(newOrigin)
        layoutLayers()
    }

    private func layoutLayers() {
        let b = bounds
        ring.path = CGPath(ellipseIn: b.insetBy(dx: 1, dy: 1), transform: nil)
        let inset = max(1.5, b.width * 0.25)
        core.frame = b.insetBy(dx: inset, dy: inset)
        core.cornerRadius = core.frame.width / 2
    }

    func setColor(_ color: NSColor, pulsing: Bool) {
        core.backgroundColor = color.cgColor
        ring.strokeColor = color.cgColor
        if pulsing {
            startPulse()
        } else {
            ring.removeAllAnimations()
            ring.opacity = 0
        }
    }

    private func startPulse() {
        guard ring.animation(forKey: "pulse") == nil else { return }
        let scale = CABasicAnimation(keyPath: "transform.scale")
        scale.fromValue = 1.0
        scale.toValue = 2.6
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 0.75
        fade.toValue = 0.0
        let group = CAAnimationGroup()
        group.animations = [scale, fade]
        group.duration = 1.6
        group.repeatCount = .infinity
        group.timingFunction = CAMediaTimingFunction(name: .easeOut)
        ring.add(group, forKey: "pulse")
        ring.opacity = 0.75
    }
}

let app = NSApplication.shared
let controller = HUDController()
app.delegate = controller
app.setActivationPolicy(.accessory)
app.run()
