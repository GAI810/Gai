import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let img = NSImage(systemSymbolName: "tray.and.arrow.down.fill",
                             accessibilityDescription: "简历入库") {
            img.isTemplate = true
            statusItem.button?.image = img
            statusItem.button?.imagePosition = .imageLeading
        }
        statusItem.button?.title = " …"
        statusItem.isVisible = true
        NSLog("status item created: \(statusItem.button != nil)")

        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "打开状态面板", action: #selector(openPanel), keyEquivalent: "o"))
        menu.addItem(NSMenuItem(title: "打开入库记录", action: #selector(openReport), keyEquivalent: "r"))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "刷新", action: #selector(refresh), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "退出", action: #selector(quit), keyEquivalent: "q"))
        statusItem.menu = menu

        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    @objc func refresh() {
        guard let url = URL(string: "http://127.0.0.1:8787/api/status") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let self = self else { return }
            var ok = false
            var today = 0
            if let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                ok = (obj["watch"] as? [String: Any])?["ok"] as? Bool ?? false
                today = obj["today"] as? Int ?? 0
            }
            DispatchQueue.main.async {
                let title = ok ? "● \(today)" : "⚠︎ \(today)"
                let attr = NSAttributedString(string: title, attributes: [
                    .font: NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .medium),
                    .foregroundColor: ok ? NSColor.systemGreen : NSColor.systemRed
                ])
                self.statusItem.button?.attributedTitle = attr
                self.statusItem.button?.toolTip = ok ? "简历入库：运行中，今日 \(today) 份" : "监听服务可能已停止"
                self.statusItem.button?.contentTintColor = ok ? NSColor.systemGreen : NSColor.systemRed
            }
        }.resume()
    }

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
               let report = files.filter({ $0.lastPathComponent.hasSuffix(".html") }).first {
                NSWorkspace.shared.open(report)
                return
            }
        }
        openPanel()
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
