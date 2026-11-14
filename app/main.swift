// main.swift: Cost Agent (Mantra), Marko's cost guard (13.11.2026).
// Marko: "Make Mantra Cost Agent in star menu. When I open it, I can see how much money is spent through all these
// processes we are doing with AI." and "are you sure you want to recognize images? This will cost you."
//
// Two faces of one program:
//   CostAgent --ask "<question>" [--timeout S]   the box: floats on top, appears where the mouse is, stays until answered;
//                                                prints YES or NO (NO by default, on Escape, and on the timeout)
//   CostAgent                                    the dashboard: money spent today / this week / this month, per provider
//                                                and per process, the questions waiting, the history of YES and NO
// The numbers come from the gate (gate/costagent.py, the port is in ~/.config/mantra-ports.json as "costagent").
// The gate is started when the dashboard opens and finds it down. Nothing here spends or holds a key.

import AppKit
import SwiftUI

let VERSION = 3

// ---------------------------------------------------------------- the gate
func gatePort() -> Int {
    let p = NSString(string: "~/.config/mantra-ports.json").expandingTildeInPath
    if let d = FileManager.default.contents(atPath: p),
       let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
       let e = j["costagent"] as? [String: Any], let n = e["port"] as? Int { return n }
    return 9047
}
func gateURL(_ path: String) -> URL { URL(string: "http://127.0.0.1:\(gatePort())\(path)")! }

func startGate() {
    let script = NSString(string: "~/Developer/COST_AGENT/gate/costagent.py").expandingTildeInPath
    guard FileManager.default.fileExists(atPath: script) else { return }
    let t = Process()
    t.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    t.arguments = ["python3", script, "serve"]
    t.standardOutput = FileHandle.nullDevice
    t.standardError = FileHandle.nullDevice
    try? t.run()
}

// ---------------------------------------------------------------- colours
extension Color {
    init(hex: String) {
        let v = UInt32(hex, radix: 16) ?? 0
        self.init(red: Double((v >> 16) & 255) / 255, green: Double((v >> 8) & 255) / 255, blue: Double(v & 255) / 255)
    }
    static let ink = Color(hex: "101010")
    static let sand = Color(hex: "E8DCC0")
    static let dimSand = Color(hex: "8F8670")
    static let amber = Color(hex: "FFB000")
    static let green = Color(hex: "5BC77A")
    static let red = Color(hex: "FF5A4D")
}
func mono(_ size: CGFloat, _ weight: Font.Weight = .bold) -> Font { .system(size: size, weight: weight, design: .monospaced) }
func usd(_ x: Double) -> String { x >= 0.01 || x == 0 ? String(format: "$%.2f", x) : String(format: "$%.4f", x) }

// ---------------------------------------------------------------- the box (--ask)
final class AskBox: NSObject, NSWindowDelegate {
    let panel: NSPanel
    var done = false
    init(text: String, timeout: Int) {
        let w: CGFloat = 520, h: CGFloat = 260
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: w, height: h), styleMask: [.titled], backing: .buffered, defer: false)
        super.init()
        panel.title = "COST AGENT"
        panel.isFloatingPanel = true
        panel.level = .screenSaver
        panel.hidesOnDeactivate = false
        panel.backgroundColor = NSColor(calibratedRed: 0.063, green: 0.063, blue: 0.063, alpha: 1)
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = NSFont.monospacedSystemFont(ofSize: 17, weight: .semibold)
        label.textColor = NSColor(calibratedRed: 0.91, green: 0.86, blue: 0.75, alpha: 1)
        label.alignment = .center
        label.frame = NSRect(x: 24, y: 90, width: w - 48, height: h - 130)
        let yes = NSButton(title: "YES", target: self, action: #selector(yes))
        let no = NSButton(title: "NO", target: self, action: #selector(no))
        yes.frame = NSRect(x: w / 2 - 170, y: 24, width: 150, height: 48)
        no.frame = NSRect(x: w / 2 + 20, y: 24, width: 150, height: 48)
        no.keyEquivalent = "\u{1b}"      // Escape is NO; Return is nothing: a YES is always a click
        for b in [yes, no] { b.bezelStyle = .rounded; b.font = NSFont.monospacedSystemFont(ofSize: 20, weight: .bold) }
        panel.contentView?.addSubview(label)
        panel.contentView?.addSubview(yes)
        panel.contentView?.addSubview(no)
        panel.delegate = self
        let m = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(m, $0.frame, false) } ?? NSScreen.main!
        var x = m.x - w / 2, y = m.y - h / 2
        x = min(max(x, screen.frame.minX + 8), screen.frame.maxX - w - 8)
        y = min(max(y, screen.frame.minY + 8), screen.frame.maxY - h - 8)
        panel.setFrameOrigin(NSPoint(x: x, y: y))
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + .seconds(max(timeout, 1))) { [weak self] in self?.finish("NO") }
    }
    @objc func yes() { finish("YES") }
    @objc func no() { finish("NO") }
    func windowWillClose(_ n: Notification) { finish("NO") }
    func finish(_ a: String) {
        if done { return }
        done = true
        print(a)
        fflush(stdout)
        exit(a == "YES" ? 0 : 1)
    }
}

// ---------------------------------------------------------------- the numbers
struct Per: Decodable { var today: Double; var week: Double; var month: Double }
struct Totals: Decodable { var today: Double; var week: Double; var month: Double }
struct Pend: Decodable, Identifiable { var id: String; var ask: String; var estimate_usd: Double; var age_s: Int }
struct Hist: Decodable, Identifiable {
    var id: String?; var ts: String?; var who: String?; var what: String?; var provider: String?
    var estimate_usd: Double?; var answer: String?; var `where`: String?
    var uid: String { (id ?? "") + (ts ?? "") }
}
struct Allow: Decodable { var daily_usd: Double; var today_usd: Double }
struct Summary: Decodable {
    var totals: Totals
    var by_provider: [String: Per]
    var by_process: [String: Per]
    var by_where: [String: Per]
    var pending: [Pend]
    var history: [Hist]
    var allowances: [String: Allow]
    var ask_above_usd: Double?
    var small_total_today: Double?
    var small_passes: [Hist]?
}

final class Model: ObservableObject {
    @Published var s: Summary?
    @Published var down = false
    var timer: Timer?
    var tried = false
    func start() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in self?.refresh() }
    }
    func refresh() {
        URLSession.shared.dataTask(with: gateURL("/summary")) { d, _, _ in
            DispatchQueue.main.async {
                if let d = d, let s = try? JSONDecoder().decode(Summary.self, from: d) { self.s = s; self.down = false }
                else {
                    self.down = true
                    if !self.tried { self.tried = true; startGate() }
                }
            }
        }.resume()
    }
    func answer(_ id: String, _ a: String) {
        var r = URLRequest(url: gateURL("/answer"))
        r.httpMethod = "POST"
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try? JSONSerialization.data(withJSONObject: ["id": id, "answer": a])
        URLSession.shared.dataTask(with: r) { _, _, _ in DispatchQueue.main.async { self.refresh() } }.resume()
    }
}

// ---------------------------------------------------------------- the dashboard
struct Big: View {
    let label: String; let v: Double
    var body: some View {
        VStack(spacing: 4) {
            Text(label).font(mono(11, .semibold)).tracking(2).foregroundColor(.dimSand)
            Text(usd(v)).font(mono(34)).foregroundColor(v > 1 ? .red : .amber)
        }.frame(maxWidth: .infinity)
    }
}

struct Table: View {
    let title: String; let rows: [String: Per]
    var body: some View {
        VStack(spacing: 6) {
            Text(title).font(mono(11, .semibold)).tracking(2).foregroundColor(.dimSand)
            Rectangle().fill(Color.dimSand.opacity(0.4)).frame(height: 1)
            HStack { Text("").frame(maxWidth: .infinity); col("TODAY"); col("WEEK"); col("MONTH") }
            if rows.isEmpty { Text("nothing spent").font(mono(12, .regular)).foregroundColor(.dimSand) }
            ForEach(rows.keys.sorted { rows[$0]!.month > rows[$1]!.month }, id: \.self) { k in
                HStack {
                    Text(k).font(mono(13)).foregroundColor(.sand).frame(maxWidth: .infinity, alignment: .center)
                    cell(rows[k]!.today); cell(rows[k]!.week); cell(rows[k]!.month)
                }
            }
        }
    }
    func col(_ t: String) -> some View { Text(t).font(mono(10, .semibold)).foregroundColor(.dimSand).frame(width: 90) }
    func cell(_ v: Double) -> some View { Text(usd(v)).font(mono(13)).foregroundColor(v > 0 ? .amber : .dimSand).frame(width: 90) }
}

struct Dashboard: View {
    @ObservedObject var m: Model
    var body: some View {
        ScrollView {
            VStack(spacing: 22) {
                Text("COST AGENT").font(mono(13, .semibold)).tracking(4).foregroundColor(.sand)
                if m.down { Text("The gate is not answering: starting it. Nothing can be spent without it.")
                    .font(mono(12, .regular)).foregroundColor(.red).multilineTextAlignment(.center) }
                if let s = m.s {
                    HStack(spacing: 10) { Big(label: "TODAY", v: s.totals.today); Big(label: "THIS WEEK", v: s.totals.week); Big(label: "THIS MONTH", v: s.totals.month) }
                    if !s.pending.isEmpty { pending(s) }
                    threshold(s)
                    Table(title: "PER PROVIDER", rows: s.by_provider)
                    Table(title: "PER PROCESS", rows: s.by_process)
                    Table(title: "WHERE IT RAN", rows: s.by_where)
                    if !s.allowances.isEmpty { allowances(s) }
                    history(s)
                    Text("The gate asks before every paid call; no answer is a NO.")
                        .font(mono(11, .regular)).foregroundColor(.dimSand).multilineTextAlignment(.center)
                } else { Text("reading the ledgers ...").font(mono(12, .regular)).foregroundColor(.dimSand) }
            }.padding(28).frame(maxWidth: .infinity)
        }.background(Color.ink)
    }
    func pending(_ s: Summary) -> some View {
        VStack(spacing: 10) {
            Text("WAITING FOR YOU").font(mono(11, .semibold)).tracking(2).foregroundColor(.red)
            ForEach(s.pending) { p in
                VStack(spacing: 8) {
                    Text(p.ask).font(mono(13, .regular)).foregroundColor(.sand).multilineTextAlignment(.center)
                    HStack(spacing: 16) {
                        Button("YES") { m.answer(p.id, "yes") }.font(mono(15))
                        Button("NO") { m.answer(p.id, "no") }.font(mono(15))
                    }
                }
            }
        }.padding(14).overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.red, lineWidth: 1))
    }
    func threshold(_ s: Summary) -> some View {
        VStack(spacing: 6) {
            Text("THE LINE").font(mono(11, .semibold)).tracking(2).foregroundColor(.dimSand)
            Text("Below \(usd(s.ask_above_usd ?? 0.10)) it passes and is recorded; from \(usd(s.ask_above_usd ?? 0.10)) up it asks \"Do you approve?\"")
                .font(mono(13)).foregroundColor(.amber).multilineTextAlignment(.center)
            Text("passed without a question today: \(usd(s.small_total_today ?? 0)) in \((s.small_passes ?? []).count) recent calls")
                .font(mono(11, .regular)).foregroundColor(.dimSand)
            ForEach(Array((s.small_passes ?? []).prefix(5)), id: \.uid) { h in
                Text("\(usd(h.estimate_usd ?? 0))  \(h.who ?? ""): \(h.what ?? "")").font(mono(11, .regular)).foregroundColor(.sand).lineLimit(1)
            }
        }
    }
    func allowances(_ s: Summary) -> some View {
        VStack(spacing: 6) {
            Text("DAILY ALLOWANCE WITHOUT A QUESTION").font(mono(11, .semibold)).tracking(2).foregroundColor(.dimSand)
            ForEach(s.allowances.keys.sorted(), id: \.self) { k in
                let a = s.allowances[k]!
                Text("\(k): \(usd(a.today_usd)) of \(usd(a.daily_usd)) today").font(mono(13)).foregroundColor(.amber)
            }
        }
    }
    func history(_ s: Summary) -> some View {
        VStack(spacing: 6) {
            Text("YOUR ANSWERS").font(mono(11, .semibold)).tracking(2).foregroundColor(.dimSand)
            Rectangle().fill(Color.dimSand.opacity(0.4)).frame(height: 1)
            if s.history.isEmpty { Text("no question asked yet").font(mono(12, .regular)).foregroundColor(.dimSand) }
            ForEach(s.history, id: \.uid) { h in
                HStack(spacing: 8) {
                    Text(String((h.ts ?? "").dropFirst(5).prefix(11)).replacingOccurrences(of: "T", with: " "))
                        .font(mono(11, .regular)).foregroundColor(.dimSand)
                    Text((h.answer ?? "").uppercased()).font(mono(12)).foregroundColor(h.answer == "yes" ? .green : .red)
                    Text(usd(h.estimate_usd ?? 0)).font(mono(12)).foregroundColor(.amber)
                    Text("\(h.who ?? ""): \(h.what ?? "")").font(mono(11, .regular)).foregroundColor(.sand).lineLimit(1)
                }
            }
        }
    }
}

// ---------------------------------------------------------------- start
final class Delegate: NSObject, NSApplicationDelegate {
    var win: NSWindow?
    let model = Model()
    func applicationDidFinishLaunching(_ n: Notification) {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 820), styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "Cost Agent (Mantra)"
        w.minSize = NSSize(width: 520, height: 480)
        w.contentView = NSHostingView(rootView: Dashboard(m: model))
        w.center()
        w.makeKeyAndOrderFront(nil)
        win = w
        model.start()
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
}

let args = CommandLine.arguments
let app = NSApplication.shared
if let i = args.firstIndex(of: "--ask"), i + 1 < args.count {
    app.setActivationPolicy(.accessory)
    var t = 120
    if let j = args.firstIndex(of: "--timeout"), j + 1 < args.count { t = Int(args[j + 1]) ?? 120 }
    let box = AskBox(text: args[i + 1], timeout: t)
    _ = box
    app.run()
} else {
    app.setActivationPolicy(.regular)
    let d = Delegate()
    app.delegate = d
    app.run()
}
