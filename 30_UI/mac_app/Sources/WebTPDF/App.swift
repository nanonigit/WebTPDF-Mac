import AppKit
import AVFoundation
import CryptoKit
import PDFKit
import SwiftUI
import UniformTypeIdentifiers
@preconcurrency import UserNotifications

@main
struct WebTPDFApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                .frame(minWidth: 980, minHeight: 680)
                .onAppear {
                    appDelegate.model = model
                    model.backfillHistoryPDFPageCounts()
                    model.requestNotificationPermission()
                    model.refreshDockerStatus()
                    model.refreshTTSModels()
                    model.refreshOllamaModels()
                    model.refreshRuntimeChecks()
                    model.refreshLibraryPDFs()
                }
        }
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    weak var model: AppModel?

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        model?.cancelJob()
    }
}

@MainActor
final class AppModel: ObservableObject {
    enum RunState {
        case idle
        case running
        case completed
        case failed
    }

    enum OutputFormat: String, CaseIterable, Identifiable {
        case mp3
        case m4b

        var id: String { rawValue }
    }

    enum PageTurnDirection: String, CaseIterable, Identifiable, Codable {
        case left
        case right

        var id: String { rawValue }

        var label: String {
            switch self {
            case .left: return "← 左キー"
            case .right: return "→ 右キー"
            }
        }
    }

    enum UILanguage: String, CaseIterable, Identifiable {
        case ja
        case en

        var id: String { rawValue }

        var label: String {
            switch self {
            case .ja: return "日本語"
            case .en: return "English"
            }
        }
    }

    enum AudioEngineMode: String, CaseIterable, Identifiable {
        case local
        case docker

        var id: String { rawValue }

        var label: String {
            switch self {
            case .local: return "Macローカル"
            case .docker: return "Docker (ebook2audiobook)"
            }
        }
    }

    enum PDFCompressionPreset: String, CaseIterable, Identifiable {
        case light
        case balanced
        case strong

        var id: String { rawValue }

        func label(_ lang: UILanguage) -> String {
            switch self {
            case .light: return lang == .ja ? "軽め (150dpi・高画質)" : "Light (150dpi)"
            case .balanced: return lang == .ja ? "標準 (120dpi・推奨)" : "Balanced (120dpi)"
            case .strong: return lang == .ja ? "強め (96dpi・最小)" : "Strong (96dpi)"
            }
        }
    }

    struct PDFQueueItem: Identifiable, Hashable {
        enum Status: String {
            case pending
            case running
            case completed
            case failed
        }

        let id = UUID()
        var url: String
        var direction: PageTurnDirection
        var status: Status = .pending
        var outputURL: URL?
        var errorMessage: String?
    }

    struct URLFetchHistoryItem: Identifiable, Codable, Hashable {
        let id: UUID
        let url: String
        let direction: PageTurnDirection
        let timestamp: Date
        let outputPath: String?
        let succeeded: Bool
        let errorMessage: String?
        var pageCount: Int?
        var expectedPages: Int?
        let lastPrintedPage: Int?
        let unverifiedGaps: Int?
        let verifiedPages: Int?

        var isKindle: Bool {
            URL(string: url)?.host?.lowercased().hasPrefix("read.amazon.") == true
        }

        var hasWarning: Bool {
            guard succeeded else { return true }
            if let unverifiedGaps { return unverifiedGaps > 0 }
            guard let pageCount, let expectedPages else { return false }
            return pageCount < expectedPages
        }
    }

    private struct KindlePageTotal: Decodable {
        let expectedPages: Int
    }

    private struct KindleVerificationProgress {
        let url: String
        let direction: PageTurnDirection
        let unchecked: Int
    }

    private struct URLFetchResult: Decodable, Sendable {
        let outputPath: String?
        let pageCount: Int
        let expectedPages: Int?
        let lastPrintedPage: Int?
        let unverifiedGaps: Int?
        let verifiedPages: Int?
        let message: String?
    }

    private struct URLFetchInterrupted: LocalizedError, Sendable {
        let result: URLFetchResult
        let message: String
        var errorDescription: String? { message }
    }

    struct RuntimeCheckItem: Identifiable {
        let id: String
        let name: String
        let ok: Bool
        let detail: String
        let actionTitle: String?
        let actionShell: String?
    }

    struct TTSModelItem: Identifiable, Decodable, Hashable {
        let id: String
        let name: String
        let engine: String
        let voice: String
        let language: String
        let languages: [String]?
        let installed: Bool

        var supportedLanguages: [String] {
            if let languages, !languages.isEmpty {
                return languages
            }
            return [language]
        }
    }

    struct OllamaModelInfo: Identifiable, Decodable, Hashable {
        struct Details: Decodable, Hashable {
            let parameter_size: String?
            let quantization_level: String?
            let family: String?
        }

        let name: String
        let size: Int64?
        let details: Details?

        var id: String { name }
    }

    private struct OllamaTagsResponse: Decodable {
        let models: [OllamaModelInfo]
    }

    enum Device: String, CaseIterable, Identifiable {
        case automatic = ""
        case cpu = "CPU"
        case cuda = "CUDA"
        case rocm = "ROCM"
        case xpu = "XPU"
        case jetson = "JETSON"

        var id: String { rawValue }

        var label: String {
            rawValue.isEmpty ? "auto / CPU" : rawValue
        }
    }

    private struct UserVisibleError: LocalizedError {
        let message: String

        var errorDescription: String? {
            message
        }
    }

    @Published var uiLanguage: UILanguage = .ja
    @Published var ebookURL: URL?
    @Published var sourceURLText = ""
    @Published var pageTurnDirection: PageTurnDirection = .right
    @Published var pdfQueue: [PDFQueueItem] = []
    @Published var urlFetchHistory: [URLFetchHistoryItem] = {
        guard let data = UserDefaults.standard.data(forKey: "urlFetchHistoryJSON"),
              let decoded = try? JSONDecoder().decode([URLFetchHistoryItem].self, from: data)
        else {
            return []
        }
        return decoded
    }()
    @Published private(set) var checkingHistoryPageCount: UUID?
    @Published private var kindleVerificationProgress: KindleVerificationProgress?
    @Published private(set) var historyPageCountMessages: [UUID: String] = [:]
    @Published var historyPageIndex: Int = 0
    @Published var isUpdatingApp: Bool = false
    @Published private(set) var lastUpdateCheckAt = UserDefaults.standard.object(forKey: "lastUpdateCheckAt") as? Date
    @Published private(set) var isRunningQueue = false
    @Published var isQueueAutoPaused = false
    @Published var squeezerTargetURL: URL?
    @Published var squeezerPreset: PDFCompressionPreset = .balanced
    @Published var squeezerInPlace = false
    @Published var audioEngineMode: AudioEngineMode = .local
    @Published var availableTTSModels: [TTSModelItem] = [
        TTSModelItem(id: "macos:Kyoko", name: "macOS標準: Kyoko (日本語・女性)", engine: "say", voice: "Kyoko", language: "jpn", languages: ["jpn"], installed: true),
        TTSModelItem(id: "edge:ja-JP-NanamiNeural", name: "Neural: Nanami (日本語・女性・高音質)", engine: "edge", voice: "ja-JP-NanamiNeural", language: "jpn", languages: ["jpn"], installed: false),
    ]
    @Published var selectedTTSModelID = "macos:Kyoko"
    @Published var customModelURLText = ""
    @Published var openAIAPIKey: String = UserDefaults.standard.string(forKey: "openAIAPIKey") ?? "" {
        didSet {
            UserDefaults.standard.set(openAIAPIKey, forKey: "openAIAPIKey")
            refreshTTSModels()
        }
    }
    @Published var openAITTSVoice: String = UserDefaults.standard.string(forKey: "openAITTSVoice") ?? "coral" {
        didSet {
            UserDefaults.standard.set(openAITTSVoice, forKey: "openAITTSVoice")
        }
    }
    @Published var elevenLabsAPIKey: String = UserDefaults.standard.string(forKey: "elevenLabsAPIKey") ?? "" {
        didSet {
            UserDefaults.standard.set(elevenLabsAPIKey, forKey: "elevenLabsAPIKey")
            refreshTTSModels()
        }
    }
    @Published var elevenLabsVoiceID: String = UserDefaults.standard.string(forKey: "elevenLabsVoiceID") ?? "21m00Tcm4TlvDq8ikWAM" {
        didSet {
            UserDefaults.standard.set(elevenLabsVoiceID, forKey: "elevenLabsVoiceID")
        }
    }
    @Published var ollamaModels: [OllamaModelInfo] = []
    @Published var selectedOllamaModelName = ""
    @Published var ollamaPullName = "gemma3:4b"
    @Published private(set) var isRefreshingOllama = false
    @Published private(set) var ollamaReachable = false
    @Published private(set) var ollamaStatusMessage = "未確認"
    @Published private(set) var isTestingOllama = false
    @Published private(set) var ollamaTestOutput = ""
    @Published var maxPagesSetting = "1000"
    @Published var customPDFSaveDirectory: URL? = {
        if let saved = UserDefaults.standard.string(forKey: "pdfSaveDirectory"), !saved.isEmpty {
            return URL(fileURLWithPath: saved, isDirectory: true)
        }
        return nil
    }()
    @Published private(set) var runtimeChecks: [RuntimeCheckItem] = []
    @Published private(set) var libraryPDFs: [URL] = []
    @Published private(set) var libraryFolder = PDFLibraryPreferences().folder
    @Published private(set) var libraryError: String?
    @Published private var hiddenRecentPDFPaths = PDFLibraryPreferences().hiddenRecentPaths
    private let libraryPreferences = PDFLibraryPreferences()

    var recentPDFs: [URL] {
        Array(libraryPDFs.lazy.filter { !self.hiddenRecentPDFPaths.contains($0.path) }.prefix(5))
    }
    @Published var voiceURL: URL?
    @Published var customModelURL: URL?
    @Published var language = "jpn"
    @Published var outputFormat: OutputFormat = .mp3
    @Published var ttsEngine = ""
    @Published var device: Device = .automatic
    @Published var dockerImage = "athomasson2/ebook2audiobook:cpu"
    @Published var dryRun = false
    @Published var sampleMode = false
    @Published var samplePageCount = "8"

    @Published private(set) var runState: RunState = .idle
    @Published private(set) var statusMessage = "Idle"
    @Published private(set) var logText = "Ready."
    @Published private(set) var artifactURL: URL?
    @Published private(set) var fetchedPDFURL: URL?
    @Published private(set) var outputDirectory: URL?
    @Published private(set) var isPlaying = false
    @Published private(set) var isFetchingURL = false
    @Published private(set) var dockerReady = false
    @Published private(set) var dockerStatus = "Checking Docker..."
    @Published private(set) var elapsedText = "-"
    @Published private(set) var progressText = "推定中"
    @Published private(set) var remainingText = "推定中"
    @Published private(set) var progressFraction: Double?

    private var process: Process?
    private var fetchProcess: Process?
    private var fetchOutputRemainder = Data()
    private var player: AVPlayer?
    private var activeOutputDirectory: URL?
    private var activeDryRun = false
    @Published private var cancelRequested = false
    private var jobStartedAt: Date?
    private var timerTask: Task<Void, Never>?
    private var preflightTask: Task<Void, Never>?
    private var fetchedFromSourceURL: String?

    private var applicationSupportRoot: URL {
        if let baseURL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first {
            return baseURL.appendingPathComponent("WebTPDF Audio Lab", isDirectory: true)
        }
        return URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("Application Support", isDirectory: true)
            .appendingPathComponent("WebTPDF Audio Lab", isDirectory: true)
    }

    private var outputRoot: URL {
        applicationSupportRoot
            .appendingPathComponent("output", isDirectory: true)
            .appendingPathComponent("audiobooks", isDirectory: true)
            .appendingPathComponent("native_jobs", isDirectory: true)
    }

    private var cacheRoot: URL {
        URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .appendingPathComponent(".cache", isDirectory: true)
            .appendingPathComponent("webtpdf", isDirectory: true)
            .appendingPathComponent("ebook2audiobook", isDirectory: true)
    }

    var fetchedPDFRoot: URL {
        if let customPDFSaveDirectory {
            return customPDFSaveDirectory
        }
        if let desktopURL = FileManager.default.urls(for: .desktopDirectory, in: .userDomainMask).first {
            return desktopURL
        }
        return URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .appendingPathComponent("Desktop", isDirectory: true)
    }

    func choosePDFSaveDirectory() {
        let panel = NSOpenPanel()
        panel.title = t("PDFの保存先フォルダを選択", "Choose PDF Save Folder")
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = fetchedPDFRoot

        guard panel.runModal() == .OK, let chosen = panel.url else {
            return
        }
        customPDFSaveDirectory = chosen
        UserDefaults.standard.set(chosen.path, forKey: "pdfSaveDirectory")
    }

    func resetPDFSaveDirectory() {
        customPDFSaveDirectory = nil
        UserDefaults.standard.removeObject(forKey: "pdfSaveDirectory")
    }

    func t(_ ja: String, _ en: String) -> String {
        uiLanguage == .ja ? ja : en
    }

    var availableLanguagesForSelectedModel: [String] {
        availableTTSModels.first(where: { $0.id == selectedTTSModelID })?.supportedLanguages ?? ["jpn", "eng"]
    }

    func languageDisplayName(_ code: String) -> String {
        switch code {
        case "jpn": return t("日本語 (jpn)", "Japanese (jpn)")
        case "eng": return t("英語 (eng)", "English (eng)")
        case "zho": return t("中国語 (zho)", "Chinese (zho)")
        case "kor": return t("韓国語 (kor)", "Korean (kor)")
        case "fra": return t("フランス語 (fra)", "French (fra)")
        case "deu": return t("ドイツ語 (deu)", "German (deu)")
        case "spa": return t("スペイン語 (spa)", "Spanish (spa)")
        default: return code
        }
    }

    func syncLanguageForSelectedModel() {
        let supported = availableLanguagesForSelectedModel
        if !supported.contains(language), let first = supported.first {
            language = first
        }
    }

    var trimmedSourceURL: String {
        sourceURLText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var canFetchURL: Bool {
        !trimmedSourceURL.isEmpty && !isRunning
    }

    var canRun: Bool {
        ebookURL != nil && !isRunning
    }

    var isRunning: Bool {
        isUpdatingApp || process != nil || preflightTask != nil || fetchProcess != nil || isFetchingURL || isRunningQueue
    }

    var activeOperationLabel: String? {
        guard isRunning else { return nil }
        if isUpdatingApp { return t("更新中", "Updating") }
        if cancelRequested { return t("停止中", "Stopping") }
        return t("実行中", "Running")
    }

    var canCancelJob: Bool {
        isRunning && !isUpdatingApp && !cancelRequested
    }

    var canEnqueueURL: Bool {
        !trimmedSourceURL.isEmpty
    }

    var hasPendingQueueItems: Bool {
        pdfQueue.contains(where: { $0.status == .pending })
    }

    var hasFailedQueueItems: Bool {
        pdfQueue.contains(where: { $0.status == .failed })
    }

    func chooseEbook() {
        if let chosen = chooseFile(allowedFileTypes: ["pdf", "epub", "mobi", "azw3"], title: "Choose Ebook") {
            ebookURL = chosen
            fetchedFromSourceURL = nil
        }
    }

    func chooseSqueezerPDF() {
        if let chosen = chooseFile(allowedFileTypes: ["pdf"], title: t("圧縮するPDFを選択", "Choose PDF to Compress")) {
            squeezerTargetURL = chosen
        }
    }

    func chooseSqueezerFolder() {
        let panel = NSOpenPanel()
        panel.title = t("一括圧縮するフォルダを選択", "Choose Folder to Compress")
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            squeezerTargetURL = url
        }
    }

    func runPDFSqueezer() {
        guard let target = squeezerTargetURL else {
            statusMessage = t("圧縮するPDFまたはフォルダを選択してください。", "Select a PDF file or folder to compress.")
            return
        }
        guard !isRunning else { return }
        guard let pythonCommand = resolvePythonCommand() else {
            fail(t("Python 3 が見つかりませんでした。実行環境からセットアップしてください。", "Python 3 not found. Please set it up from Runtime."))
            return
        }
        guard let scriptURL = Bundle.main.url(forResource: "pdf_squeezer", withExtension: "py") else {
            fail("Bundled script pdf_squeezer.py was not found.")
            return
        }

        cancelRequested = false
        runState = .running
        statusMessage = t("PDFを圧縮中...", "Compressing PDF(s)...")
        progressFraction = nil
        progressText = t("圧縮中", "Compressing")
        remainingText = "-"
        logText = "[Squeezer] Target: \(target.path)\n"
        startProgressClock()

        var args = [
            scriptURL.path,
            "--input", target.path,
            "--preset", squeezerPreset.rawValue,
        ]
        if squeezerInPlace {
            args.append("--in-place")
        }

        runPythonSubprocess(
            pythonCommand: pythonCommand,
            arguments: args,
            workingDirectory: applicationSupportRoot
        ) { [weak self] exitCode in
            guard let self else { return }
            self.stopProgressClock()
            if self.cancelRequested {
                self.runState = .idle
                self.statusMessage = "Canceled"
                self.progressText = "-"
                self.remainingText = "-"
                return
            }
            if exitCode == 0 {
                self.runState = .completed
                self.statusMessage = self.t("PDF圧縮が完了しました", "PDF compression completed")
                self.progressText = "100%"
                self.remainingText = "00:00"
                self.refreshLibraryPDFs()
                self.sendCompletionNotification(
                    title: self.t("PDF圧縮完了", "PDF Compression Complete"),
                    body: target.lastPathComponent
                )
                self.startQueueIfAutoRunEnabled()
            } else {
                self.fail(self.t("PDF圧縮に失敗しました", "PDF compression failed") + " (exit \(exitCode)).")
            }
        }
    }

    func clearEbook() {
        ebookURL = nil
        fetchedPDFURL = nil
        fetchedFromSourceURL = nil
    }

    func openFetchedPDF() {
        guard let fetchedPDFURL else {
            return
        }
        NSWorkspace.shared.open(fetchedPDFURL)
    }

    func launchLoginBrowser() {
        let chromePath = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        guard FileManager.default.isExecutableFile(atPath: chromePath) else {
            fail("Google Chrome が見つかりませんでした: \(chromePath)")
            return
        }

        let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
        let profileDir = URL(fileURLWithPath: home, isDirectory: true)
            .appendingPathComponent(".cache", isDirectory: true)
            .appendingPathComponent("ebookconverter-chrome", isDirectory: true)

        do {
            try FileManager.default.createDirectory(at: profileDir, withIntermediateDirectories: true)
        } catch {
            fail("Chrome プロファイルフォルダの作成に失敗しました: \(error.localizedDescription)")
            return
        }

        var arguments = [
            "--remote-debugging-port=9223",
            "--user-data-dir=\(profileDir.path)",
        ]
        if let parsed = URL(string: trimmedSourceURL),
           let scheme = parsed.scheme?.lowercased(),
           (scheme == "http" || scheme == "https"),
           parsed.host != nil {
            arguments.append(trimmedSourceURL)
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: chromePath)
        proc.arguments = arguments

        do {
            try proc.run()
            statusMessage = "ログイン用ブラウザ起動済み"
            appendLog("\nLaunched debuggable Chrome (--remote-debugging-port=9223).\nログイン後、「URLからPDF作成」を押してください。\n")
        } catch {
            fail("ログイン用ブラウザの起動に失敗しました: \(error.localizedDescription)")
        }
    }

    func chooseVoice() {
        voiceURL = chooseFile(allowedFileTypes: ["wav", "mp3", "m4a", "flac", "ogg"], title: "Choose Voice Sample")
    }

    func chooseCustomModel() {
        customModelURL = chooseFile(allowedFileTypes: ["zip"], title: "Choose Custom Model")
    }

    func clearVoice() {
        voiceURL = nil
    }

    func clearCustomModel() {
        customModelURL = nil
    }

    func refreshDockerStatus() {
        guard let docker = resolveDockerCommand() else {
            dockerReady = false
            dockerStatus = "Docker not found"
            return
        }

        dockerStatus = "Checking Docker..."
        Task { [weak self] in
            guard let self else {
                return
            }

            if let version = await self.dockerInfoVersion(dockerCommand: docker) {
                dockerReady = true
                dockerStatus = "Docker ready \(version)"
            } else {
                dockerReady = false
                dockerStatus = "Docker is installed but not ready"
            }
        }
    }

    func enqueueCurrentURL() {
        let urlString = trimmedSourceURL
        guard !urlString.isEmpty else { return }
        guard let parsed = URL(string: urlString),
              let scheme = parsed.scheme?.lowercased(),
              (scheme == "http" || scheme == "https"),
              parsed.host != nil
        else {
            statusMessage = t("有効な http:// または https:// のURLを入力してください。", "Enter a valid http:// or https:// URL.")
            return
        }

        pdfQueue.append(PDFQueueItem(url: urlString, direction: pageTurnDirection))
        sourceURLText = ""
        isQueueAutoPaused = false
        if !isRunning {
            statusMessage = t("キューに追加し、自動実行を開始しました (\(pdfQueue.count)件)", "Added to queue and started auto-run (\(pdfQueue.count))")
            startQueue()
        } else {
            statusMessage = t("キューに追加しました (\(pdfQueue.count)件)", "Added to queue (\(pdfQueue.count))")
        }
    }

    func startQueueIfAutoRunEnabled() {
        guard !isQueueAutoPaused, hasPendingQueueItems, !isRunning else { return }
        startQueue()
    }

    func pauseQueueAutoRun() {
        isQueueAutoPaused = true
        if isRunningQueue {
            statusMessage = t("現在の項目が完了したら自動実行を一時停止します", "Will pause auto-run after current item finishes")
        } else {
            statusMessage = t("自動実行を一時停止しました", "Queue auto-run paused")
        }
    }

    func resumeQueueAutoRun() {
        isQueueAutoPaused = false
        if hasPendingQueueItems && !isRunning {
            startQueue()
        } else {
            statusMessage = t("自動実行を再開しました", "Queue auto-run resumed")
        }
    }

    func removeQueueItem(_ id: UUID) {
        pdfQueue.removeAll { $0.id == id && $0.status != .running }
    }

    func clearFinishedQueueItems() {
        pdfQueue.removeAll { $0.status == .completed || $0.status == .failed }
    }

    func recordURLFetchHistory(
        url: String,
        direction: PageTurnDirection,
        outputURL: URL?,
        succeeded: Bool,
        errorMessage: String? = nil,
        pageCount: Int? = nil,
        expectedPages: Int? = nil,
        lastPrintedPage: Int? = nil,
        unverifiedGaps: Int? = nil,
        verifiedPages: Int? = nil
    ) {
        let entry = URLFetchHistoryItem(
            id: UUID(),
            url: url,
            direction: direction,
            timestamp: Date(),
            outputPath: outputURL?.path,
            succeeded: succeeded,
            errorMessage: errorMessage,
            pageCount: pageCount,
            expectedPages: expectedPages,
            lastPrintedPage: lastPrintedPage,
            unverifiedGaps: unverifiedGaps,
            verifiedPages: verifiedPages
        )
        urlFetchHistory.removeAll { $0.url == url && $0.direction == direction }
        urlFetchHistory.insert(entry, at: 0)
        historyPageIndex = 0
        if urlFetchHistory.count > 100 {
            urlFetchHistory = Array(urlFetchHistory.prefix(100))
        }
        saveURLFetchHistory()
    }

    private func recordURLFetchFailure(url: String, direction: PageTurnDirection, error: Error) {
        let result = (error as? URLFetchInterrupted)?.result
        recordURLFetchHistory(
            url: url, direction: direction, outputURL: nil, succeeded: false,
            errorMessage: error.localizedDescription, pageCount: result?.pageCount, expectedPages: result?.expectedPages
        )
    }

    func backfillHistoryPDFPageCounts() {
        let missing = urlFetchHistory.compactMap { entry -> (UUID, String)? in
            guard entry.pageCount == nil, let path = entry.outputPath else { return nil }
            return (entry.id, path)
        }
        guard !missing.isEmpty else { return }
        Task {
            let counts = await Task.detached(priority: .utility) {
                var result: [UUID: Int] = [:]
                for (id, path) in missing {
                    if let count = PDFDocument(url: URL(fileURLWithPath: path))?.pageCount, count > 0 {
                        result[id] = count
                    }
                }
                return result
            }.value
            var changed = false
            for index in urlFetchHistory.indices where urlFetchHistory[index].pageCount == nil {
                if let count = counts[urlFetchHistory[index].id] {
                    urlFetchHistory[index].pageCount = count
                    changed = true
                }
            }
            if changed { saveURLFetchHistory() }
        }
    }

    func checkKindleTotal(for item: URLFetchHistoryItem) {
        guard item.isKindle, checkingHistoryPageCount == nil, !isRunning else { return }
        let failure = t(
            "総ページ数を取得できませんでした。専用ChromeでKindleにログインし、本文にページ番号が表示されるか確認してください。",
            "Could not read the total. Sign in to Kindle in the login browser and check that the reader shows page numbers."
        )
        guard let python = resolvePythonCommand(), let script = resolveURLFetcherScript() else {
            historyPageCountMessages[item.id] = failure
            return
        }
        checkingHistoryPageCount = item.id
        historyPageCountMessages[item.id] = nil
        let resultURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("WebTPDF-total-\(UUID().uuidString).json")
        let lookup = Process()
        lookup.executableURL = URL(fileURLWithPath: python)
        lookup.arguments = [script.path, "--url", item.url, "--inspect-kindle-total", "--result-json", resultURL.path]
        lookup.environment = mergedSubprocessEnvironment()
        lookup.standardOutput = FileHandle.nullDevice
        lookup.standardError = FileHandle.nullDevice
        lookup.terminationHandler = { [weak self] finished in
            Task { @MainActor in
                defer { try? FileManager.default.removeItem(at: resultURL) }
                guard let self else { return }
                self.checkingHistoryPageCount = nil
                guard let index = self.urlFetchHistory.firstIndex(where: { $0.id == item.id }) else { return }
                if finished.terminationStatus == 0,
                   let data = try? Data(contentsOf: resultURL),
                   let result = try? JSONDecoder().decode(KindlePageTotal.self, from: data),
                   result.expectedPages > 0 {
                    self.urlFetchHistory[index].expectedPages = result.expectedPages
                    self.saveURLFetchHistory()
                } else {
                    self.historyPageCountMessages[item.id] = failure
                }
            }
        }
        do {
            try lookup.run()
        } catch {
            checkingHistoryPageCount = nil
            historyPageCountMessages[item.id] = failure
        }
    }

    func historyItem(forPDF pdfURL: URL) -> URLFetchHistoryItem? {
        let targetPath = pdfURL.standardizedFileURL.path
        return urlFetchHistory.first { item in
            guard let outputPath = item.outputPath, !outputPath.isEmpty else { return false }
            let itemURL = URL(fileURLWithPath: outputPath)
            return itemURL.standardizedFileURL.path == targetPath
                || itemURL.lastPathComponent == pdfURL.lastPathComponent
        }
    }

    func activeHistoryQueueStatus(_ entry: URLFetchHistoryItem) -> PDFQueueItem.Status? {
        pdfQueue.first {
            $0.url == entry.url && $0.direction == entry.direction
                && ($0.status == .pending || $0.status == .running)
        }?.status
    }

    func remainingKindleIntervals(for entry: URLFetchHistoryItem) -> Int? {
        guard let progress = kindleVerificationProgress,
              progress.url == entry.url, progress.direction == entry.direction else { return nil }
        return progress.unchecked
    }

    func redownloadFromHistory(_ item: URLFetchHistoryItem) {
        guard !pdfQueue.contains(where: {
            $0.url == item.url && $0.direction == item.direction
                && ($0.status == .pending || $0.status == .running)
        }) else {
            return
        }
        pdfQueue.append(PDFQueueItem(url: item.url, direction: item.direction))
        isQueueAutoPaused = false
        if !isRunning {
            statusMessage = t("履歴から再ダウンロードを開始しました", "Started re-download from history")
            startQueue()
        } else {
            statusMessage = t("履歴からキューに追加しました (\(pdfQueue.count)件)", "Added from history to queue (\(pdfQueue.count))")
        }
    }

    func removeURLFetchHistoryItem(_ id: UUID) {
        urlFetchHistory.removeAll { $0.id == id }
        let totalPages = max(1, (urlFetchHistory.count + 9) / 10)
        if historyPageIndex >= totalPages {
            historyPageIndex = max(0, totalPages - 1)
        }
        saveURLFetchHistory()
    }

    func clearURLFetchHistory() {
        urlFetchHistory.removeAll()
        historyPageIndex = 0
        saveURLFetchHistory()
    }

    private func saveURLFetchHistory() {
        if let encoded = try? JSONEncoder().encode(urlFetchHistory) {
            UserDefaults.standard.set(encoded, forKey: "urlFetchHistoryJSON")
        }
    }

    func retryQueueItem(_ id: UUID) {
        guard let idx = pdfQueue.firstIndex(where: { $0.id == id && $0.status != .running }) else {
            return
        }
        pdfQueue[idx].status = .pending
        pdfQueue[idx].errorMessage = nil
        isQueueAutoPaused = false
        if !isRunning {
            startQueue()
        }
    }

    func retryFailedQueueItems() {
        var resetCount = 0
        for idx in pdfQueue.indices where pdfQueue[idx].status == .failed {
            pdfQueue[idx].status = .pending
            pdfQueue[idx].errorMessage = nil
            resetCount += 1
        }
        if resetCount > 0 {
            isQueueAutoPaused = false
            if !isRunning {
                startQueue()
            }
        }
    }

    func startQueue() {
        guard hasPendingQueueItems, !isRunning else { return }

        cancelRequested = false
        isQueueAutoPaused = false
        isRunningQueue = true
        runState = .running
        progressFraction = nil
        progressText = t("連続処理中", "Queueing")
        remainingText = "-"
        logText = "Starting PDF queue (\(pdfQueue.filter { $0.status == .pending }.count) pending item(s))...\n"
        startProgressClock()

        preflightTask = Task { [weak self] in
            guard let self else { return }
            var processedCount = 0
            var failedCount = 0

            while !self.cancelRequested,
                  !self.isQueueAutoPaused,
                  let nextIndex = self.pdfQueue.firstIndex(where: { $0.status == .pending }) {
                processedCount += 1
                let item = self.pdfQueue[nextIndex]
                self.pdfQueue[nextIndex].status = .running
                self.pdfQueue[nextIndex].errorMessage = nil
                self.pdfQueue[nextIndex].outputURL = nil
                self.statusMessage = self.t("キュー実行中 (\(processedCount)): \(item.url)", "Queue (\(processedCount)): \(item.url)")
                self.appendLog("\n[Queue #\(processedCount)] (\(item.direction.label)) \(item.url)\n")

                let outcome: Result<(URL, URLFetchResult), Error>
                do {
                    outcome = .success(try await self.performURLFetch(urlString: item.url, direction: item.direction))
                } catch {
                    outcome = .failure(error)
                }
                // Earlier rows can be removed while awaiting the subprocess.
                guard let index = self.pdfQueue.firstIndex(where: { $0.id == item.id }) else { continue }
                switch outcome {
                case let .success((mergedURL, result)):
                    self.pdfQueue[index].status = .completed
                    self.pdfQueue[index].outputURL = mergedURL
                    self.ebookURL = mergedURL
                    self.fetchedPDFURL = mergedURL
                    self.fetchedFromSourceURL = item.url
                    self.outputDirectory = mergedURL.deletingLastPathComponent()
                    self.recordURLFetchHistory(url: item.url, direction: item.direction, outputURL: mergedURL, succeeded: true, pageCount: result.pageCount, expectedPages: result.expectedPages, lastPrintedPage: result.lastPrintedPage, unverifiedGaps: result.unverifiedGaps, verifiedPages: result.verifiedPages)
                    self.appendLog("[Queue #\(processedCount)] Saved -> \(mergedURL.path)\n")
                case let .failure(error):
                    if !self.cancelRequested { failedCount += 1 }
                    self.pdfQueue[index].status = self.cancelRequested ? .pending : .failed
                    self.pdfQueue[index].errorMessage = self.cancelRequested ? nil : error.localizedDescription
                    self.recordURLFetchFailure(url: item.url, direction: item.direction, error: error)
                    self.appendLog("[Queue #\(processedCount)] \(error.localizedDescription)\n")
                }
            }

            self.preflightTask = nil
            self.isRunningQueue = false
            self.stopProgressClock()
            if self.cancelRequested {
                self.runState = .idle
                self.statusMessage = "Canceled"
            } else if self.isQueueAutoPaused && self.hasPendingQueueItems {
                self.runState = .idle
                let remaining = self.pdfQueue.filter { $0.status == .pending }.count
                self.statusMessage = self.t("自動実行を一時停止しました（残り\(remaining)件）", "Queue paused (\(remaining) pending)")
                self.progressText = self.t("一時停止", "Paused")
            } else if failedCount > 0 {
                self.runState = .failed
                self.statusMessage = self.t(
                    "キュー終了（成功 \(processedCount - failedCount)件、失敗 \(failedCount)件）",
                    "Queue finished (\(processedCount - failedCount) succeeded, \(failedCount) failed)"
                )
                self.progressText = "100%"
                self.remainingText = "00:00"
            } else {
                self.runState = .completed
                self.statusMessage = self.t("キュー実行完了 (\(processedCount)件処理)", "Queue completed (\(processedCount) item(s))")
                self.progressText = "100%"
                self.remainingText = "00:00"
                self.sendCompletionNotification(
                    title: self.t("キュー実行完了", "Queue Complete"),
                    body: self.t("\(processedCount)件の処理が完了しました。", "Processed \(processedCount) item(s).")
                )
            }
        }
    }

    func fetchPDFfromURL() {
        let urlString = trimmedSourceURL
        guard !urlString.isEmpty else {
            statusMessage = "ソースURLを入力してください。"
            return
        }
        sourceURLText = ""

        let direction = pageTurnDirection
        cancelRequested = false
        runState = .running
        isFetchingURL = true
        statusMessage = "URLからPDFを取得中..."
        progressFraction = nil
        progressText = "取得中"
        remainingText = "-"
        logText = "Fetching PDF from URL: \(urlString)\n"
        recordURLFetchHistory(
            url: urlString,
            direction: direction,
            outputURL: nil,
            succeeded: false,
            errorMessage: t("取得中...", "Fetching...")
        )
        startProgressClock()

        preflightTask = Task { [weak self] in
            guard let self else {
                return
            }

            do {
                let (mergedURL, result) = try await self.performURLFetch(urlString: urlString, direction: direction)
                self.preflightTask = nil
                self.isFetchingURL = false
                self.stopProgressClock()
                self.ebookURL = mergedURL
                self.fetchedPDFURL = mergedURL
                self.fetchedFromSourceURL = urlString
                self.outputDirectory = mergedURL.deletingLastPathComponent()
                self.runState = .completed
                self.statusMessage = "PDF取得完了"
                self.progressText = "100%"
                self.remainingText = "00:00"
                self.refreshLibraryPDFs()
                self.recordURLFetchHistory(url: urlString, direction: direction, outputURL: mergedURL, succeeded: true, pageCount: result.pageCount, expectedPages: result.expectedPages, lastPrintedPage: result.lastPrintedPage, unverifiedGaps: result.unverifiedGaps, verifiedPages: result.verifiedPages)
                self.appendLog("\nMerged PDF saved to: \(mergedURL.path)\n")
                self.sendCompletionNotification(
                    title: self.t("PDF作成完了", "PDF Created"),
                    body: mergedURL.lastPathComponent
                )
                self.startQueueIfAutoRunEnabled()
            } catch {
                self.preflightTask = nil
                self.isFetchingURL = false
                self.recordURLFetchFailure(url: urlString, direction: direction, error: error)
                if self.cancelRequested {
                    self.finishCanceledPreflight()
                } else {
                    self.fail(error.localizedDescription)
                }
                self.startQueueIfAutoRunEnabled()
            }
        }
    }

    func startJob() {
        guard let ebookURL else {
            statusMessage = "先に電子書籍ファイルを選択するか、URLからPDFを作成してください。"
            return
        }

        startLocalAudiobookJob(ebookURL: ebookURL)
    }

    func refreshTTSModels() {
        guard let pythonCommand = resolvePythonCommand(),
              let scriptURL = resolveLocalAudiobookScript()
        else {
            return
        }

        let env = mergedSubprocessEnvironment()
        Task { [weak self] in
            let decoded: [TTSModelItem]? = await Task.detached {
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: pythonCommand)
                proc.arguments = [scriptURL.path, "--list-models"]
                proc.environment = env
                let pipe = Pipe()
                proc.standardOutput = pipe
                proc.standardError = Pipe()
                do {
                    try proc.run()
                    let data = pipe.fileHandleForReading.readDataToEndOfFile()
                    proc.waitUntilExit()
                    guard proc.terminationStatus == 0 else { return nil }
                    return try JSONDecoder().decode([TTSModelItem].self, from: data)
                } catch {
                    return nil
                }
            }.value

            guard let self, let decoded else { return }
            self.availableTTSModels = decoded
            if !decoded.contains(where: { $0.id == self.selectedTTSModelID }),
               let first = decoded.first {
                self.selectedTTSModelID = first.id
            }
        }
    }

    func refreshOllamaModels() {
        isRefreshingOllama = true
        ollamaStatusMessage = "Ollamaモデル取得中..."

        Task { [weak self] in
            guard let self else { return }
            do {
                guard let url = URL(string: "http://127.0.0.1:11434/api/tags") else { return }
                var request = URLRequest(url: url)
                request.timeoutInterval = 3
                let (data, response) = try await URLSession.shared.data(for: request)
                guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                    self.ollamaReachable = false
                    self.ollamaModels = []
                    self.ollamaStatusMessage = "Ollama API に接続できません (127.0.0.1:11434)"
                    self.isRefreshingOllama = false
                    return
                }
                let decoded = try JSONDecoder().decode(OllamaTagsResponse.self, from: data)
                self.ollamaReachable = true
                self.ollamaModels = decoded.models
                if !decoded.models.contains(where: { $0.name == self.selectedOllamaModelName }) {
                    self.selectedOllamaModelName = decoded.models.first?.name ?? ""
                }
                self.ollamaStatusMessage = decoded.models.isEmpty ? "起動中 (モデル未DL)" : "\(decoded.models.count)件のモデルを検出"
            } catch {
                self.ollamaReachable = false
                self.ollamaModels = []
                self.ollamaStatusMessage = "Ollama が起動していません（Ollama.app 起動後に再読み込み）"
            }
            self.isRefreshingOllama = false
            self.refreshRuntimeChecks()
        }
    }

    func pullOllamaModel(_ name: String) {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            statusMessage = "ダウンロードするOllamaモデル名を入力してください。"
            return
        }
        guard let ollamaCommand = resolveOllamaCommand() else {
            fail("ollama コマンドが見つかりませんでした。Ollama をインストールしてください。")
            return
        }

        cancelRequested = false
        runState = .running
        statusMessage = "Ollamaモデルをプル中: \(trimmed)"
        progressFraction = nil
        progressText = "DL中"
        remainingText = "-"
        logText = "$ \(ollamaCommand) pull \(trimmed)\n\n"
        startProgressClock()

        runPythonSubprocess(
            pythonCommand: ollamaCommand,
            arguments: ["pull", trimmed],
            workingDirectory: applicationSupportRoot
        ) { [weak self] exitCode in
            guard let self else { return }
            self.stopProgressClock()
            if exitCode == 0 {
                self.runState = .completed
                self.statusMessage = "Ollamaモデル取得完了: \(trimmed)"
                self.progressText = "100%"
                self.remainingText = "00:00"
                self.selectedOllamaModelName = trimmed
                self.refreshOllamaModels()
            } else {
                self.fail("Ollamaモデルのダウンロードに失敗しました (exit \(exitCode))。")
            }
        }
    }

    func refreshRuntimeChecks() {
        let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
        let pythonPath = resolvePythonCommand()
        let ffmpegPath = findExecutable([
            "\(home)/.local/bin/ffmpeg",
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
        ])
        let sayOk = FileManager.default.isExecutableFile(atPath: "/usr/bin/say")
        let chromeOk = FileManager.default.isExecutableFile(atPath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        let ollamaPath = resolveOllamaCommand()
        let ollamaAppExists = FileManager.default.fileExists(atPath: "/Applications/Ollama.app")
        let ollamaInstalled = (ollamaPath != nil) || ollamaAppExists
        let brewPath = findExecutable(["/opt/homebrew/bin/brew", "/usr/local/bin/brew"])
        let py = pythonPath ?? "python3"
        let brewCmd = brewPath ?? "brew"
        let ollamaStartCmd = ollamaAppExists
            ? "open -a Ollama"
            : "nohup \"\(ollamaPath ?? "ollama")\" serve >/tmp/webtpdf_ollama.log 2>&1 &"
        let ollamaWaitCmd = "for i in {1..16}; do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 0.5; done"
        let ollamaActionShell = ollamaInstalled
            ? "\(ollamaStartCmd) && \(ollamaWaitCmd)"
            : "\"\(brewCmd)\" install ollama && nohup \"$(\"\(brewCmd)\" --prefix)/bin/ollama\" serve >/tmp/webtpdf_ollama.log 2>&1 & \(ollamaWaitCmd)"

        let ffmpegInstallShell = """
        if [ -x /opt/homebrew/bin/brew ] || [ -x /usr/local/bin/brew ]; then
          BREW=$([ -x /opt/homebrew/bin/brew ] && echo /opt/homebrew/bin/brew || echo /usr/local/bin/brew)
          "$BREW" install ffmpeg
        else
          mkdir -p "$HOME/.local/bin"
          curl -fsSL "https://evermeet.cx/ffmpeg/getrelease/zip" -o /tmp/webtpdf_ffmpeg.zip
          unzip -o /tmp/webtpdf_ffmpeg.zip -d "$HOME/.local/bin"
          chmod +x "$HOME/.local/bin/ffmpeg"
          rm -f /tmp/webtpdf_ffmpeg.zip
        fi
        """
        let ffmpegUpdateShell = brewPath != nil
            ? "\"\(brewPath!)\" upgrade ffmpeg || true"
            : ffmpegInstallShell

        runtimeChecks = [
            RuntimeCheckItem(
                id: "python",
                name: t("Python 3 & 必要パッケージ", "Python 3 & Required Packages"),
                ok: pythonPath != nil,
                detail: pythonPath ?? t("未検出", "Not found"),
                actionTitle: t("導入/更新", "Install/Update"),
                actionShell: "\"\(py)\" -m pip install --upgrade requests beautifulsoup4 pypdf pymupdf playwright pillow && \"\(py)\" -m playwright install chromium"
            ),
            RuntimeCheckItem(
                id: "chrome",
                name: "Google Chrome (CDP)",
                ok: chromeOk,
                detail: chromeOk ? t("利用可能", "Available") : t("未検出", "Not found"),
                actionTitle: chromeOk ? t("ログイン用を開く", "Open Browser") : t("導入", "Install"),
                actionShell: chromeOk ? nil : "\"\(brewPath ?? "brew")\" install --cask google-chrome"
            ),
            RuntimeCheckItem(
                id: "say",
                name: t("macOS 音声合成 (say)", "macOS Speech (say)"),
                ok: sayOk,
                detail: sayOk ? "/usr/bin/say" : t("未検出", "Not found"),
                actionTitle: nil,
                actionShell: nil
            ),
            RuntimeCheckItem(
                id: "ffmpeg",
                name: t("ffmpeg (MP3/M4B変換)", "ffmpeg (MP3/M4B)"),
                ok: ffmpegPath != nil,
                detail: ffmpegPath ?? t("未検出", "Not found"),
                actionTitle: t(ffmpegPath == nil ? "導入" : "更新", ffmpegPath == nil ? "Install" : "Update"),
                actionShell: ffmpegPath == nil ? ffmpegInstallShell : ffmpegUpdateShell
            ),
            RuntimeCheckItem(
                id: "ollama",
                name: t("Ollama (AI接続)", "Ollama (AI Connection)"),
                ok: ollamaReachable,
                detail: ollamaStatusMessage,
                actionTitle: ollamaInstalled ? t("起動", "Launch") : t("導入", "Install"),
                actionShell: ollamaActionShell
            ),
        ]
    }

    func runRuntimeAction(_ item: RuntimeCheckItem) {
        if item.id == "chrome", item.actionShell == nil {
            launchLoginBrowser()
            return
        }
        guard let shellCmd = item.actionShell else { return }
        runMaintenanceShell(title: item.name, shellScript: shellCmd)
    }

    func installOrUpdateAllDependencies() {
        let brewPath = findExecutable(["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]) ?? "brew"
        let py = resolvePythonCommand() ?? "python3"
        let script = """
        set -e
        "\(py)" -m pip install --upgrade requests beautifulsoup4 pypdf pymupdf playwright pillow
        "\(py)" -m playwright install chromium
        if command -v "\(brewPath)" >/dev/null 2>&1; then
          "\(brewPath)" list ffmpeg >/dev/null 2>&1 && "\(brewPath)" upgrade ffmpeg || "\(brewPath)" install ffmpeg || true
          if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
            if ! command -v ollama >/dev/null 2>&1; then
              "\(brewPath)" install ollama || true
            fi
            if [ -d "/Applications/Ollama.app" ]; then
              open -a Ollama
            elif command -v ollama >/dev/null 2>&1; then
              nohup ollama serve >/tmp/webtpdf_ollama.log 2>&1 &
            fi
            for i in {1..16}; do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 0.5; done
          fi
        fi
        """
        runMaintenanceShell(title: t("必要パッケージ一括導入/更新", "Install/Update Dependencies"), shellScript: script)
    }

    private func runMaintenanceShell(title: String, shellScript: String) {
        guard !isRunning else { return }
        cancelRequested = false
        runState = .running
        statusMessage = "\(title)..."
        progressFraction = nil
        progressText = t("実行中", "Running")
        remainingText = "-"
        logText = "$ \(shellScript)\n\n"
        startProgressClock()

        runPythonSubprocess(
            pythonCommand: "/bin/zsh",
            arguments: ["-c", shellScript],
            workingDirectory: applicationSupportRoot
        ) { [weak self] exitCode in
            guard let self else { return }
            self.stopProgressClock()
            if exitCode == 0 {
                self.runState = .completed
                self.statusMessage = "\(title) \(self.t("完了", "Done"))"
                self.progressText = "100%"
                self.remainingText = "00:00"
                self.cachedPythonPath = nil
                self.refreshTTSModels()
                self.refreshOllamaModels()
                self.refreshRuntimeChecks()
            } else {
                self.fail("\(title) \(self.t("に失敗しました", "failed")) (exit \(exitCode)).")
            }
        }
    }

    func resolveRepoRoot() -> URL? {
        // Walk up from .../40_Ops/apps/WebTPDF Mac.app -> repo root
        let candidateFromBundle = Bundle.main.bundleURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        if FileManager.default.fileExists(atPath: candidateFromBundle.appendingPathComponent("30_UI/mac_app/build_mac_app.sh").path) {
            return candidateFromBundle
        }
        let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
        let fallback = URL(fileURLWithPath: home, isDirectory: true)
            .appendingPathComponent("Git", isDirectory: true)
            .appendingPathComponent("WebTPDF-Mac", isDirectory: true)
        if FileManager.default.fileExists(atPath: fallback.appendingPathComponent("30_UI/mac_app/build_mac_app.sh").path) {
            return fallback
        }
        return nil
    }

    var appVersionDisplay: String {
        let ver = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1.0"
        let sha = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "-"
        return "v\(ver) (\(sha))"
    }

    func updateAndRelaunchApp() {
        guard let repoRoot = resolveRepoRoot() else {
            fail("WebTPDF-Mac リポジトリが見つかりませんでした。")
            return
        }

        guard !isRunning,
              let updater = Bundle.main.url(forResource: "update_mac_app", withExtension: "sh") else {
            return
        }
        let installedApp = Bundle.main.bundleURL.path
        let currentDisplay = appVersionDisplay
        let currentSHA = (Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        cancelRequested = false
        isUpdatingApp = true
        runState = .running
        statusMessage = t("アップデート中 (\(currentDisplay) -> 最新版)...", "Updating (\(currentDisplay) -> latest)...")
        progressFraction = nil
        progressText = t("更新中", "Updating")
        remainingText = "-"
        logText = "Current app version: \(currentDisplay)\nUpdating WebTPDF Mac from \(repoRoot.path)...\n"
        startProgressClock()

        runPythonSubprocess(
            pythonCommand: "/bin/zsh",
            arguments: [updater.path, currentSHA, installedApp],
            workingDirectory: repoRoot
        ) { [weak self] exitCode in
            guard let self else { return }
            self.stopProgressClock()
            self.isUpdatingApp = false
            if exitCode == 0 || exitCode == 10 || exitCode == 11 {
                let checkedAt = Date()
                self.lastUpdateCheckAt = checkedAt
                UserDefaults.standard.set(checkedAt, forKey: "lastUpdateCheckAt")
            }
            switch exitCode {
            case 0:
                UserDefaults.standard.set(currentDisplay, forKey: "lastUpdatedFromVersion")
                self.runState = .completed
                self.statusMessage = self.t("アップデート完了・再起動中...", "Update complete. Relaunching...")
                let relaunch = Process()
                relaunch.executableURL = URL(fileURLWithPath: "/bin/zsh")
                relaunch.arguments = ["-c", #"sleep 1 && /usr/bin/open "$1""#, "webtpdf-relaunch", installedApp]
                do {
                    try relaunch.run()
                    NSApp.terminate(nil)
                } catch {
                    self.fail(self.t("更新しました。アプリを開き直してください。", "Updated. Reopen the app to use the new build."))
                }
            case 10:
                self.runState = .completed
                self.statusMessage = self.t("すでに最新版です (\(currentDisplay))", "Already up to date (\(currentDisplay))")
            case 11:
                self.runState = .completed
                self.statusMessage = self.t("現在の修正を含む更新版はまだありません。", "No update containing the current build is available yet.")
                self.appendLog("\n\(self.statusMessage)\n")
            default:
                self.fail(self.t("アップデートに失敗しました (exit \(exitCode))。", "Update failed (exit \(exitCode))."))
            }
        }
    }

    func chooseLibraryFolder() {
        let panel = NSOpenPanel()
        panel.title = t("PDFライブラリのフォルダーを選択", "Choose PDF Library Folder")
        panel.prompt = t("選択", "Choose")
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = libraryFolder
        guard panel.runModal() == .OK, let folder = panel.url else { return }
        libraryFolder = folder.standardizedFileURL
        libraryPreferences.saveFolder(libraryFolder)
        refreshLibraryPDFs()
    }

    func refreshLibraryPDFs() {
        do {
            libraryPDFs = try PDFLibraryPreferences.pdfs(in: libraryFolder)
            libraryError = nil
        } catch {
            libraryPDFs = []
            libraryError = error.localizedDescription
        }
    }

    func hideRecentPDF(_ url: URL) {
        hideRecentPDFs([url])
    }

    func clearRecentPDFs() {
        hideRecentPDFs(libraryPDFs)
    }

    private func hideRecentPDFs(_ urls: [URL]) {
        hiddenRecentPDFPaths.formUnion(urls.map(\.path))
        libraryPreferences.saveHiddenRecentPaths(hiddenRecentPDFPaths)
    }

    func clearLog() {
        logText = "Ready."
    }

    func copyLogToPasteboard() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(logText, forType: .string)
    }

    func testOllamaInference() {
        let modelName = selectedOllamaModelName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !modelName.isEmpty else {
            ollamaTestOutput = "先にモデルを選択してください。"
            return
        }
        isTestingOllama = true
        ollamaTestOutput = "推論テスト中..."

        Task { [weak self] in
            guard let self else { return }
            do {
                guard let url = URL(string: "http://127.0.0.1:11434/api/generate") else { return }
                var request = URLRequest(url: url)
                request.httpMethod = "POST"
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.timeoutInterval = 30
                let body: [String: Any] = [
                    "model": modelName,
                    "prompt": "Reply with OK.",
                    "stream": false,
                ]
                request.httpBody = try JSONSerialization.data(withJSONObject: body)
                let started = Date()
                let (data, _) = try await URLSession.shared.data(for: request)
                let elapsed = Date().timeIntervalSince(started)
                if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let resp = json["response"] as? String {
                    let evalCount = json["eval_count"] as? Int ?? 0
                    let evalDuration = (json["eval_duration"] as? Double ?? 0) / 1e9
                    let tps = evalDuration > 0 ? Double(evalCount) / evalDuration : 0
                    self.ollamaTestOutput = String(format: "応答: %@ (%.2fs / %.1f tok/s)", resp.trimmingCharacters(in: .whitespacesAndNewlines), elapsed, tps)
                } else {
                    self.ollamaTestOutput = "応答を解析できませんでした。"
                }
            } catch {
                self.ollamaTestOutput = "テスト失敗: \(error.localizedDescription)"
            }
            self.isTestingOllama = false
        }
    }

    private func resolveOllamaCommand() -> String? {
        findExecutable([
            "/usr/local/bin/ollama",
            "/opt/homebrew/bin/ollama",
            "/Applications/Ollama.app/Contents/Resources/ollama",
        ])
    }

    func downloadModelTarget(_ target: String) {
        let trimmed = target.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            statusMessage = "ダウンロードするモデルまたはURLを指定してください。"
            return
        }
        guard let pythonCommand = resolvePythonCommand(),
              let scriptURL = resolveLocalAudiobookScript()
        else {
            fail("python3 または local_audiobook.py が見つかりませんでした。")
            return
        }

        cancelRequested = false
        runState = .running
        statusMessage = "音声モデルをダウンロード中..."
        progressFraction = nil
        progressText = "DL中"
        remainingText = "-"
        logText = "Downloading TTS model: \(trimmed)\n"
        startProgressClock()

        runPythonSubprocess(
            pythonCommand: pythonCommand,
            arguments: [scriptURL.path, "--download-model", trimmed],
            workingDirectory: applicationSupportRoot
        ) { [weak self] exitCode in
            guard let self else { return }
            self.stopProgressClock()
            if exitCode == 0 {
                self.runState = .completed
                self.statusMessage = "モデルダウンロード完了"
                self.progressText = "100%"
                self.remainingText = "00:00"
                self.refreshTTSModels()
            } else {
                self.fail("モデルダウンロードに失敗しました (exit \(exitCode))。")
            }
        }
    }

    private func startLocalAudiobookJob(ebookURL: URL) {
        guard let pythonCommand = resolvePythonCommand(),
              let scriptURL = resolveLocalAudiobookScript()
        else {
            fail("python3 または local_audiobook.py が見つかりませんでした。")
            return
        }

        let jobID = makeJobID()
        let outputDirectory = outputRoot.appendingPathComponent(jobID, isDirectory: true)
        do {
            try prepareRuntimeDirectories(outputDirectory: outputDirectory)
        } catch {
            fail("Failed to prepare runtime directories: \(error.localizedDescription)")
            return
        }

        activeOutputDirectory = outputDirectory
        activeDryRun = dryRun
        cancelRequested = false
        artifactURL = nil
        self.outputDirectory = outputDirectory
        player = nil
        isPlaying = false

        let conversionEbookURL: URL
        do {
            conversionEbookURL = try prepareEbookForConversion(ebookURL: ebookURL, outputDirectory: outputDirectory)
        } catch {
            fail(error.localizedDescription)
            return
        }

        let arguments = [
            scriptURL.path,
            "--ebook", conversionEbookURL.path,
            "--output-dir", outputDirectory.path,
            "--language", language.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "jpn" : language.trimmingCharacters(in: .whitespacesAndNewlines),
            "--output-format", outputFormat.rawValue,
            "--model", selectedTTSModelID,
        ]

        logText = "$ \(shellJoined([pythonCommand] + arguments))\n\n"
        runState = .running
        statusMessage = "ローカル音声変換中..."
        startProgressClock()

        if dryRun {
            finishJob(exitCode: 0)
            return
        }

        runPythonSubprocess(
            pythonCommand: pythonCommand,
            arguments: arguments,
            workingDirectory: applicationSupportRoot
        ) { [weak self] exitCode in
            self?.finishJob(exitCode: exitCode)
        }
    }

    private func mergedSubprocessEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = appPath()
        environment["PYTHONUNBUFFERED"] = "1"
        let openAI = openAIAPIKey.trimmingCharacters(in: .whitespacesAndNewlines)
        if !openAI.isEmpty {
            environment["OPENAI_API_KEY"] = openAI
        }
        let openAIVoice = openAITTSVoice.trimmingCharacters(in: .whitespacesAndNewlines)
        if !openAIVoice.isEmpty {
            environment["OPENAI_TTS_VOICE"] = openAIVoice
        }
        let elevenKey = elevenLabsAPIKey.trimmingCharacters(in: .whitespacesAndNewlines)
        if !elevenKey.isEmpty {
            environment["ELEVENLABS_API_KEY"] = elevenKey
        }
        let elevenVoice = elevenLabsVoiceID.trimmingCharacters(in: .whitespacesAndNewlines)
        if !elevenVoice.isEmpty {
            environment["ELEVENLABS_VOICE_ID"] = elevenVoice
        }
        return environment
    }

    private func runPythonSubprocess(
        pythonCommand: String,
        arguments: [String],
        workingDirectory: URL,
        onFinish: @escaping @MainActor (Int32) -> Void
    ) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: pythonCommand)
        proc.arguments = arguments
        proc.currentDirectoryURL = workingDirectory

        proc.environment = mergedSubprocessEnvironment()

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let chunk = String(data: data, encoding: .utf8) else {
                return
            }
            Task { @MainActor in
                self?.appendLog(chunk)
            }
        }

        proc.terminationHandler = { [weak self] terminated in
            pipe.fileHandleForReading.readabilityHandler = nil
            Task { @MainActor in
                self?.process = nil
                onFinish(terminated.terminationStatus)
            }
        }

        do {
            try proc.run()
            self.process = proc
        } catch {
            isUpdatingApp = false
            fail("プロセスの起動に失敗しました: \(error.localizedDescription)")
        }
    }

    private func startJobWithResolvedEbook(_ ebookURL: URL) {

        let resolvedDockerCommand = resolveDockerCommand()
        guard dryRun || resolvedDockerCommand != nil else {
            fail("Docker not found. Install and start Docker Desktop, then press Docker再確認.")
            return
        }
        let dockerCommand = resolvedDockerCommand ?? "docker"

        if device != .automatic,
           device != .cpu,
           dockerImage.trimmingCharacters(in: .whitespacesAndNewlines) == "athomasson2/ebook2audiobook:cpu" {
            fail("--device \(device.rawValue) requires a matching GPU/runtime Docker image.")
            return
        }

        activeOutputDirectory = nil
        activeDryRun = dryRun
        cancelRequested = false
        artifactURL = nil
        outputDirectory = nil
        player = nil
        isPlaying = false

        if dryRun {
            startConversionJob(dockerCommand: dockerCommand, ebookURL: ebookURL)
            return
        }

        preflightTask?.cancel()
        runState = .running
        statusMessage = "Checking Docker..."
        progressText = "推定中"
        remainingText = "推定中"
        progressFraction = nil
        elapsedText = "-"
        logText = "Checking Docker Desktop...\n"

        preflightTask = Task { [weak self] in
            guard let self else {
                return
            }

            let ready = await self.waitForDockerReady(dockerCommand: dockerCommand)
            preflightTask = nil

            if Task.isCancelled || cancelRequested {
                finishCanceledPreflight()
                return
            }

            guard ready else {
                fail("Docker Desktop did not become ready. Start Docker Desktop, wait until it finishes starting, then try again.")
                return
            }

            startConversionJob(dockerCommand: dockerCommand, ebookURL: ebookURL)
        }
    }

    private func startConversionJob(dockerCommand: String, ebookURL: URL) {
        let jobID = makeJobID()
        let outputDirectory = outputRoot.appendingPathComponent(jobID, isDirectory: true)

        do {
            try prepareRuntimeDirectories(outputDirectory: outputDirectory)
        } catch {
            fail("Failed to prepare runtime directories: \(error.localizedDescription)")
            return
        }

        activeOutputDirectory = outputDirectory
        activeDryRun = dryRun
        cancelRequested = false
        artifactURL = nil
        self.outputDirectory = outputDirectory
        player = nil
        isPlaying = false

        let conversionEbookURL: URL
        do {
            conversionEbookURL = try prepareEbookForConversion(ebookURL: ebookURL, outputDirectory: outputDirectory)
        } catch {
            fail(error.localizedDescription)
            return
        }

        let command = buildCommand(dockerCommand: dockerCommand, ebookURL: conversionEbookURL, outputDirectory: outputDirectory)
        if logText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || logText == "Ready." {
            logText = "$ \(shellJoined(command))\n\n"
        } else {
            appendLog("\n$ \(shellJoined(command))\n\n")
        }
        runState = .running
        statusMessage = dryRun ? "Dry-run running..." : "Converting..."
        startProgressClock()

        if dryRun {
            finishJob(exitCode: 0)
            return
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: command[0])
        process.arguments = Array(command.dropFirst())
        process.currentDirectoryURL = applicationSupportRoot

        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = appPath()
        environment["PYTHONUNBUFFERED"] = "1"
        environment["NODE_DISABLE_COLORS"] = "1"
        process.environment = environment

        let outputPipe = Pipe()
        process.standardOutput = outputPipe
        process.standardError = outputPipe
        outputPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let chunk = String(data: data, encoding: .utf8) else {
                return
            }
            Task { @MainActor in
                self?.appendLog(chunk)
            }
        }

        process.terminationHandler = { [weak self] terminatedProcess in
            outputPipe.fileHandleForReading.readabilityHandler = nil
            Task { @MainActor in
                self?.finishJob(exitCode: terminatedProcess.terminationStatus)
            }
        }

        do {
            try process.run()
            self.process = process
        } catch {
            fail("Failed to start conversion: \(error.localizedDescription)")
        }
    }

    func cancelJob() {
        guard canCancelJob else { return }
        cancelRequested = true
        isQueueAutoPaused = true
        statusMessage = t("停止中…", "Stopping…")
        // Completion handlers keep ownership of the task until it has actually stopped.
        preflightTask?.cancel()
        if let fetchProcess, fetchProcess.isRunning {
            fetchProcess.terminate()
        }
        if let process, process.isRunning {
            process.terminate()
        }
    }

    func togglePlayback() {
        guard let artifactURL else {
            return
        }

        if isPlaying {
            player?.pause()
            isPlaying = false
            return
        }

        player = AVPlayer(url: artifactURL)
        player?.play()
        isPlaying = true
    }

    func openArtifact() {
        guard let artifactURL else {
            return
        }
        NSWorkspace.shared.open(artifactURL)
    }

    func openOutputDirectory() {
        guard let outputDirectory else {
            NSWorkspace.shared.open(outputRoot)
            return
        }
        NSWorkspace.shared.open(outputDirectory)
    }

    private func chooseFile(allowedFileTypes: [String], title: String) -> URL? {
        let panel = NSOpenPanel()
        panel.title = title
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowedContentTypes = allowedFileTypes.map { UTType(filenameExtension: $0) ?? .data }
        return panel.runModal() == .OK ? panel.url : nil
    }

    private func buildCommand(dockerCommand: String, ebookURL: URL, outputDirectory: URL) -> [String] {
        let image = dockerImage.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? "athomasson2/ebook2audiobook:cpu"
            : dockerImage.trimmingCharacters(in: .whitespacesAndNewlines)
        let modelsDirectory = cacheRoot.appendingPathComponent("models", isDirectory: true)
        let tmpDirectory = cacheRoot.appendingPathComponent("tmp", isDirectory: true)

        var command = [
            dockerCommand,
            "run",
            "--rm",
            "-i",
            "-e",
            "USER=root",
        ]
        command += dockerDeviceArgs()
        command += mountArgs(hostURL: ebookURL.deletingLastPathComponent(), containerPath: "/mnt/ebook", readOnly: true)
        command += mountArgs(hostURL: outputDirectory, containerPath: "/app/audiobooks")
        command += mountArgs(hostURL: modelsDirectory, containerPath: "/app/models")
        command += mountArgs(hostURL: tmpDirectory, containerPath: "/app/tmp")

        if let voiceURL {
            command += mountArgs(hostURL: voiceURL.deletingLastPathComponent(), containerPath: "/mnt/voice", readOnly: true)
        }

        if let customModelURL {
            command += mountArgs(hostURL: customModelURL.deletingLastPathComponent(), containerPath: "/mnt/custom_model", readOnly: true)
        }

        command += [
            image,
            "--script_mode",
            "full_docker",
            "--headless",
            "--ebook",
            "/mnt/ebook/\(ebookURL.lastPathComponent)",
            "--language",
            language.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "jpn" : language,
            "--output_dir",
            "/app/audiobooks",
            "--output_format",
            outputFormat.rawValue,
        ]

        let engine = ttsEngine.trimmingCharacters(in: .whitespacesAndNewlines)
        if !engine.isEmpty {
            command += ["--tts_engine", engine]
        }

        if device != .automatic {
            command += ["--device", device.rawValue]
        }

        if let voiceURL {
            command += ["--voice", "/mnt/voice/\(voiceURL.lastPathComponent)"]
        }

        if let customModelURL {
            command += ["--custom_model", "/mnt/custom_model/\(customModelURL.lastPathComponent)"]
        }

        return command
    }

    private func prepareEbookForConversion(ebookURL: URL, outputDirectory: URL) throws -> URL {
        guard sampleMode else {
            return ebookURL
        }

        guard ebookURL.pathExtension.lowercased() == "pdf" else {
            throw UserVisibleError(message: "短い範囲だけ試す機能はPDF入力だけに対応しています。")
        }

        let rawPageCount = samplePageCount.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let requestedPageCount = Int(rawPageCount), requestedPageCount > 0 else {
            throw UserVisibleError(message: "試用ページ数は1以上の整数で入力してください。")
        }

        return try makeSamplePDF(
            originalURL: ebookURL,
            outputDirectory: outputDirectory,
            requestedPageCount: requestedPageCount
        )
    }

    private func makeSamplePDF(originalURL: URL, outputDirectory: URL, requestedPageCount: Int) throws -> URL {
        guard let sourceDocument = PDFDocument(url: originalURL) else {
            throw UserVisibleError(message: "PDFを読み込めませんでした: \(originalURL.path)")
        }

        let availablePageCount = sourceDocument.pageCount
        guard availablePageCount > 0 else {
            throw UserVisibleError(message: "PDFにページがありません: \(originalURL.path)")
        }

        let pageCount = min(requestedPageCount, availablePageCount)
        let sampleDocument = PDFDocument()

        for pageIndex in 0..<pageCount {
            guard let sourcePage = sourceDocument.page(at: pageIndex),
                  let copiedPage = sourcePage.copy() as? PDFPage
            else {
                throw UserVisibleError(message: "PDFの\(pageIndex + 1)ページ目をコピーできませんでした。")
            }
            sampleDocument.insert(copiedPage, at: pageIndex)
        }

        let originalName = originalURL.deletingPathExtension().lastPathComponent
        let safeBaseName = originalName.replacingOccurrences(
            of: #"[^A-Za-z0-9._-]+"#,
            with: "_",
            options: .regularExpression
        )
        let fileName = "sample_first_\(pageCount)_pages_\(safeBaseName.isEmpty ? "ebook" : safeBaseName).pdf"
        let sampleURL = outputDirectory.appendingPathComponent(fileName)

        guard sampleDocument.write(to: sampleURL) else {
            throw UserVisibleError(message: "試用PDFを書き出せませんでした: \(sampleURL.path)")
        }

        appendLog("Sample mode: using first \(pageCount) of \(availablePageCount) PDF pages.\n")
        appendLog("Sample PDF: \(sampleURL.path)\n")
        return sampleURL
    }

    private func prepareRuntimeDirectories(outputDirectory: URL) throws {
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(
            at: cacheRoot.appendingPathComponent("models", isDirectory: true),
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: cacheRoot.appendingPathComponent("tmp", isDirectory: true),
            withIntermediateDirectories: true
        )
    }

    private func resolveDockerCommand() -> String? {
        findExecutable([
            "/usr/local/bin/docker",
            "/opt/homebrew/bin/docker",
            "/Applications/Docker.app/Contents/Resources/bin/docker",
        ])
    }

    private var cachedPythonPath: String?

    private func resolvePythonCommand() -> String? {
        if let cachedPythonPath, FileManager.default.isExecutableFile(atPath: cachedPythonPath) {
            return cachedPythonPath
        }

        let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
        var candidates: [String] = []

        let pyenvVersionsDir = URL(fileURLWithPath: home, isDirectory: true)
            .appendingPathComponent(".pyenv/versions", isDirectory: true)
        if let versions = try? FileManager.default.contentsOfDirectory(atPath: pyenvVersionsDir.path) {
            for version in versions.sorted(by: { $0.compare($1, options: .numeric) == .orderedDescending }) {
                candidates.append(pyenvVersionsDir.appendingPathComponent("\(version)/bin/python3").path)
            }
        }

        candidates += [
            "\(home)/.pyenv/shims/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]

        // Prefer a Python interpreter that has the required PDF/scraping modules installed
        for candidate in candidates where FileManager.default.isExecutableFile(atPath: candidate) {
            let probe = Process()
            probe.executableURL = URL(fileURLWithPath: candidate)
            probe.arguments = ["-c", "import requests, bs4, pypdf, fitz, playwright, PIL"]
            probe.standardOutput = Pipe()
            probe.standardError = Pipe()
            if (try? probe.run()) != nil {
                probe.waitUntilExit()
                if probe.terminationStatus == 0 {
                    cachedPythonPath = candidate
                    return candidate
                }
            }
        }

        return findExecutable(candidates)
    }

    private func resolveURLFetcherScript() -> URL? {
        if let resourceURL = Bundle.main.resourceURL?.appendingPathComponent("url_pdf_fetcher.py"),
           FileManager.default.isExecutableFile(atPath: resourceURL.path) || FileManager.default.fileExists(atPath: resourceURL.path) {
            return resourceURL
        }
        return nil
    }

    private func resolveLocalAudiobookScript() -> URL? {
        if let resourceURL = Bundle.main.resourceURL?.appendingPathComponent("local_audiobook.py"),
           FileManager.default.isExecutableFile(atPath: resourceURL.path) || FileManager.default.fileExists(atPath: resourceURL.path) {
            return resourceURL
        }
        return nil
    }

    private func performURLFetch(urlString: String, direction: PageTurnDirection? = nil) async throws -> (URL, URLFetchResult) {
        guard let parsed = URL(string: urlString),
              let scheme = parsed.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              parsed.host != nil
        else {
            throw UserVisibleError(message: "有効な http:// または https:// のURLを入力してください。")
        }

        guard let pythonCommand = resolvePythonCommand() else {
            throw UserVisibleError(message: "python3 が見つかりませんでした。")
        }

        guard let scriptURL = resolveURLFetcherScript() else {
            throw UserVisibleError(message: "url_pdf_fetcher.py がアプリ内に見つかりませんでした。")
        }

        try FileManager.default.createDirectory(at: fetchedPDFRoot, withIntermediateDirectories: true)
        let hostPart = (parsed.host ?? "web").replacingOccurrences(
            of: #"[^A-Za-z0-9._-]+"#,
            with: "_",
            options: .regularExpression
        )
        let outputURL = fetchedPDFRoot.appendingPathComponent("\(makeJobID())_\(hostPart).pdf")

        let resolvedDirection = direction ?? pageTurnDirection
        let checkpointURL: URL?
        if let host = parsed.host?.lowercased(), host.hasPrefix("read.amazon.") {
            let source = "\(resolvedDirection.rawValue)\n\(urlString)"
            let key = SHA256.hash(data: Data(source.utf8)).map { String(format: "%02x", $0) }.joined()
            checkpointURL = applicationSupportRoot
                .appendingPathComponent("KindleCheckpoints", isDirectory: true)
                .appendingPathComponent("\(key).sqlite3")
        } else {
            checkpointURL = nil
        }

        let resultURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("WebTPDF-\(UUID().uuidString).json")

        return try await withCheckedThrowingContinuation { continuation in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: pythonCommand)
            let maxPages = Int(maxPagesSetting.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 1000
            var arguments = [
                scriptURL.path,
                "--url", urlString,
                "--output", outputURL.path,
                "--direction", resolvedDirection.rawValue,
                "--max-pages", String(max(1, maxPages)),
                "--auto-name",
                "--result-json", resultURL.path,
            ]
            if let checkpointURL {
                arguments += ["--kindle-checkpoint", checkpointURL.path]
            }
            proc.arguments = arguments
            proc.currentDirectoryURL = fetchedPDFRoot

            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = appPath()
            environment["PYTHONUNBUFFERED"] = "1"
            proc.environment = environment

            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = pipe
            pipe.fileHandleForReading.readabilityHandler = { [weak self, weak proc] handle in
                let data = handle.availableData
                guard !data.isEmpty else { return }
                DispatchQueue.main.async {
                    guard let self, let proc, self.fetchProcess === proc else { return }
                    self.appendURLFetchOutput(data, url: urlString, direction: resolvedDirection)
                }
            }

            proc.terminationHandler = { [weak self] terminated in
                pipe.fileHandleForReading.readabilityHandler = nil
                DispatchQueue.main.async {
                    if let self, !self.fetchOutputRemainder.isEmpty {
                        self.appendLog(String(decoding: self.fetchOutputRemainder, as: UTF8.self))
                    }
                    self?.fetchOutputRemainder = Data()
                    self?.kindleVerificationProgress = nil
                    self?.fetchProcess = nil
                    let result = (try? Data(contentsOf: resultURL)).flatMap {
                        try? JSONDecoder().decode(URLFetchResult.self, from: $0)
                    }
                    try? FileManager.default.removeItem(at: resultURL)
                    if terminated.terminationStatus == 0,
                       let result, let path = result.outputPath,
                       FileManager.default.fileExists(atPath: path) {
                        continuation.resume(returning: (URL(fileURLWithPath: path), result))
                    } else if let result, let reason = result.message {
                        let canResume = checkpointURL.map {
                            FileManager.default.fileExists(atPath: $0.path)
                        } ?? false
                        let retry = canResume
                            ? (self?.t(" 履歴の「再ダウンロード」で再開できます。", " Use Re-download in history to resume.") ?? "")
                            : ""
                        continuation.resume(throwing: URLFetchInterrupted(result: result, message: reason + retry))
                    } else if self?.cancelRequested == true {
                        let message = self?.t("キャンセル", "Canceled") ?? "Canceled"
                        continuation.resume(throwing: UserVisibleError(message: message))
                    } else if let checkpointURL, FileManager.default.fileExists(atPath: checkpointURL.path) {
                        let message = self?.t(
                            "取得が中断されました。履歴の「再ダウンロード」で続きから再開します。",
                            "Capture interrupted. Use Re-download in history to resume."
                        ) ?? "Capture interrupted. Use Re-download in history to resume."
                        continuation.resume(throwing: UserVisibleError(message: message))
                    } else {
                        continuation.resume(throwing: UserVisibleError(message: "URLからのPDF取得に失敗しました (exit \(terminated.terminationStatus))。ログを確認してください。"))
                    }
                }
            }

            do {
                self.fetchProcess = proc
                try proc.run()
            } catch {
                self.fetchProcess = nil
                continuation.resume(throwing: UserVisibleError(message: "PDF取得プロセスの起動に失敗しました: \(error.localizedDescription)"))
            }
        }
    }

    private func appendURLFetchOutput(_ data: Data, url: String, direction: PageTurnDirection) {
        fetchOutputRemainder.append(data)
        guard let end = fetchOutputRemainder.lastIndex(of: 10) else { return }
        let output = String(decoding: fetchOutputRemainder[...end], as: UTF8.self)
        fetchOutputRemainder = Data(fetchOutputRemainder[fetchOutputRemainder.index(after: end)...])

        let prefix = "KINDLE_UNVERIFIED_GAPS="
        var log = ""
        for line in output.split(separator: "\n", omittingEmptySubsequences: false).dropLast() {
            if line.hasPrefix(prefix), let count = Int(line.dropFirst(prefix.count)), count >= 0 {
                kindleVerificationProgress = KindleVerificationProgress(url: url, direction: direction, unchecked: count)
            } else {
                log += line + "\n"
            }
        }
        if !log.isEmpty { appendLog(log) }
    }

    private func waitForDockerReady(dockerCommand: String) async -> Bool {
        if let version = await dockerInfoVersion(dockerCommand: dockerCommand) {
            dockerReady = true
            dockerStatus = "Docker ready \(version)"
            appendLog("Docker ready \(version).\n")
            return true
        }

        dockerReady = false
        dockerStatus = "Starting Docker Desktop..."
        statusMessage = "Starting Docker Desktop..."
        appendLog("Docker is installed but the daemon is not running. Opening Docker Desktop...\n")
        openDockerDesktop()

        for attempt in 1...90 {
            if Task.isCancelled || cancelRequested {
                return false
            }

            try? await Task.sleep(for: .seconds(2))

            if let version = await dockerInfoVersion(dockerCommand: dockerCommand) {
                dockerReady = true
                dockerStatus = "Docker ready \(version)"
                statusMessage = "Docker ready"
                appendLog("Docker ready \(version).\n")
                return true
            }

            let elapsedSeconds = attempt * 2
            dockerStatus = "Waiting for Docker Desktop... \(elapsedSeconds)s"
            statusMessage = "Waiting for Docker Desktop..."
            if attempt == 1 || elapsedSeconds % 10 == 0 {
                appendLog("Still waiting for Docker Desktop... \(elapsedSeconds)s\n")
            }
        }

        dockerReady = false
        dockerStatus = "Docker is installed but not ready"
        return false
    }

    private func dockerInfoVersion(dockerCommand: String) async -> String? {
        let path = appPath()
        return await Task.detached(priority: .utility) {
            Self.runDockerInfoVersion(dockerCommand: dockerCommand, path: path)
        }.value
    }

    private nonisolated static func runDockerInfoVersion(dockerCommand: String, path: String) -> String? {
        let process = Process()
        let pipe = Pipe()
        let semaphore = DispatchSemaphore(value: 0)

        process.executableURL = URL(fileURLWithPath: dockerCommand)
        process.arguments = ["info", "--format", "{{.ServerVersion}}"]
        process.standardOutput = pipe
        process.standardError = pipe
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = path
        process.environment = environment
        process.terminationHandler = { _ in
            semaphore.signal()
        }

        do {
            try process.run()
        } catch {
            return nil
        }

        if semaphore.wait(timeout: .now() + 5) == .timedOut {
            process.terminate()
            return nil
        }

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard process.terminationStatus == 0 else {
            return nil
        }

        let version = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return version.isEmpty ? nil : version
    }

    private func openDockerDesktop() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        process.arguments = ["-ga", "Docker"]
        process.standardOutput = nil
        process.standardError = nil
        try? process.run()
    }

    private func dockerDeviceArgs() -> [String] {
        switch device {
        case .cuda:
            return ["--gpus", "all"]
        case .rocm:
            return ["--device=/dev/kfd", "--device=/dev/dri"]
        case .xpu:
            return ["--device=/dev/dri"]
        case .jetson:
            return ["--runtime", "nvidia"]
        case .automatic, .cpu:
            return []
        }
    }

    private func mountArgs(hostURL: URL, containerPath: String, readOnly: Bool = false) -> [String] {
        let suffix = readOnly ? ":ro" : ""
        return ["-v", "\(hostURL.path):\(containerPath)\(suffix)"]
    }

    private func finishJob(exitCode: Int32) {
        process = nil
        stopProgressClock()
        defer { startQueueIfAutoRunEnabled() }

        if cancelRequested {
            runState = .idle
            statusMessage = "Canceled"
            progressText = "-"
            remainingText = "-"
            appendLog("\nCanceled.\n")
            return
        }

        if exitCode != 0 {
            runState = .failed
            statusMessage = "Failed (exit \(exitCode))"
            remainingText = "-"
            appendLog("\nFailed with exit \(exitCode).\n")
            return
        }

        if activeDryRun {
            runState = .completed
            statusMessage = "Dry-run completed"
            progressText = "dry-run"
            remainingText = "-"
            appendLog("\nDry-run completed.\n")
            return
        }

        guard let activeOutputDirectory else {
            runState = .failed
            statusMessage = "Output directory was not recorded."
            return
        }

        if let artifact = findAudioArtifact(in: activeOutputDirectory) {
            artifactURL = artifact
            runState = .completed
            statusMessage = "Generated \(artifact.lastPathComponent)"
            progressFraction = 1
            progressText = "100%"
            remainingText = "00:00"
            sendCompletionNotification(
                title: t("音声変換完了", "Audio Conversion Complete"),
                body: artifact.lastPathComponent
            )
            return
        }

        runState = .failed
        statusMessage = "Completed, but no audio file was found."
        remainingText = "-"
    }

    private func findAudioArtifact(in directory: URL) -> URL? {
        let extensions = Set(["aac", "flac", "m4a", "m4b", "mp3", "ogg", "opus", "wav"])
        guard let enumerator = FileManager.default.enumerator(
            at: directory,
            includingPropertiesForKeys: [.contentModificationDateKey, .isRegularFileKey]
        ) else {
            return nil
        }

        var bestURL: URL?
        var bestDate = Date.distantPast

        for case let url as URL in enumerator {
            guard extensions.contains(url.pathExtension.lowercased()) else {
                continue
            }

            let values = try? url.resourceValues(forKeys: [.contentModificationDateKey, .isRegularFileKey])
            guard values?.isRegularFile == true else {
                continue
            }

            let date = values?.contentModificationDate ?? Date.distantPast
            if date > bestDate {
                bestDate = date
                bestURL = url
            }
        }

        return bestURL
    }

    private func findExecutable(_ candidates: [String]) -> String? {
        for candidate in candidates where FileManager.default.isExecutableFile(atPath: candidate) {
            return candidate
        }
        return nil
    }

    private func appPath() -> String {
        let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
        return [
            "\(home)/.local/bin",
            "/usr/local/bin",
            "/opt/homebrew/bin",
            "\(home)/.pyenv/shims",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ].joined(separator: ":")
    }

    private func makeJobID() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return "\(formatter.string(from: Date()))-\(UUID().uuidString.prefix(8))"
    }

    private func appendLog(_ chunk: String) {
        logText += chunk
        if logText.count > 20000 {
            logText = String(logText.suffix(20000))
        }
        updateProgress(from: chunk)
        updateTimingLabels()
    }

    func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    func sendCompletionNotification(title: String, body: String) {
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            let authorized = settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
            if authorized {
                let content = UNMutableNotificationContent()
                content.title = title
                content.body = body
                content.sound = .default
                let req = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
                center.add(req, withCompletionHandler: nil)
            } else {
                Task.detached {
                    let script = """
                    on run args
                        display notification (item 2 of args) with title ("WebTPDF Mac — " & item 1 of args) sound name "Glass"
                    end run
                    """
                    let proc = Process()
                    proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
                    proc.arguments = ["-e", script, "--", title, body]
                    do {
                        try proc.run()
                    } catch {
                        return
                    }
                    proc.waitUntilExit()
                }
            }
        }
    }

    private func fail(_ message: String) {
        stopProgressClock()
        preflightTask?.cancel()
        preflightTask = nil
        process = nil
        runState = .failed
        statusMessage = message
        remainingText = "-"
        appendLog("\n\(message)\n")
    }

    private func finishCanceledPreflight() {
        stopProgressClock()
        preflightTask = nil
        process = nil
        runState = .idle
        statusMessage = "Canceled"
        progressText = "-"
        remainingText = "-"
        appendLog("\nCanceled.\n")
    }

    private func startProgressClock() {
        timerTask?.cancel()
        jobStartedAt = Date()
        elapsedText = "00:00"
        progressFraction = nil
        progressText = "推定中"
        remainingText = "推定中"

        timerTask = Task { [weak self] in
            while !Task.isCancelled {
                await MainActor.run {
                    self?.updateTimingLabels()
                }
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    private func stopProgressClock() {
        timerTask?.cancel()
        timerTask = nil
        updateTimingLabels()
    }

    private func updateTimingLabels() {
        guard let jobStartedAt else {
            elapsedText = "-"
            return
        }

        let elapsed = max(0, Date().timeIntervalSince(jobStartedAt))
        elapsedText = formatDuration(elapsed)

        guard runState == .running else {
            return
        }

        guard let progressFraction, progressFraction > 0 else {
            progressText = "推定中"
            remainingText = "推定中"
            return
        }

        let clampedProgress = min(max(progressFraction, 0), 1)
        progressText = "\(Int((clampedProgress * 100).rounded()))%"

        if clampedProgress >= 1 {
            remainingText = "00:00"
            return
        }

        let estimatedTotal = elapsed / clampedProgress
        remainingText = formatDuration(max(0, estimatedTotal - elapsed))
    }

    private func updateProgress(from chunk: String) {
        if let sentenceProgress = lastSentenceProgress(in: chunk) {
            progressFraction = max(progressFraction ?? 0, sentenceProgress)
        }
    }

    private func lastSentenceProgress(in text: String) -> Double? {
        let pattern = #"(?m)(?:^|\r|\n)\s*([0-9]{1,3}(?:\.[0-9]+)?)%:\s*:?\s*([0-9]{1,7})/([0-9]{1,7})(?![0-9])"#
        guard let regex = try? NSRegularExpression(pattern: pattern) else {
            return nil
        }

        let nsRange = NSRange(text.startIndex..<text.endIndex, in: text)
        let matches = regex.matches(in: text, range: nsRange)
        for match in matches.reversed() {
            guard
                let percentRange = Range(match.range(at: 1), in: text),
                let currentRange = Range(match.range(at: 2), in: text),
                let totalRange = Range(match.range(at: 3), in: text),
                let percent = Double(text[percentRange]),
                let current = Double(text[currentRange]),
                let total = Double(text[totalRange]),
                total > 0,
                current >= 0,
                current <= total,
                (0...100).contains(percent)
            else {
                continue
            }

            let ratio = current / total
            return min(max(max(ratio, percent / 100), 0), 1)
        }

        return nil
    }

    private func formatDuration(_ seconds: TimeInterval) -> String {
        let totalSeconds = max(0, Int(seconds.rounded()))
        let hours = totalSeconds / 3600
        let minutes = (totalSeconds % 3600) / 60
        let seconds = totalSeconds % 60

        if hours > 0 {
            return String(format: "%d:%02d:%02d", hours, minutes, seconds)
        }
        return String(format: "%02d:%02d", minutes, seconds)
    }

    private func shellJoined(_ command: [String]) -> String {
        command.map(shellQuoted).joined(separator: " ")
    }

    private func shellQuoted(_ value: String) -> String {
        if value.range(of: #"^[A-Za-z0-9_./:=@%+-]+$"#, options: .regularExpression) != nil {
            return value
        }
        return "'\(value.replacingOccurrences(of: "'", with: "'\\''"))'"
    }
}

private struct VisibleScrollView<Content: View>: View {
    let content: Content
    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }
    var body: some View {
        if #available(macOS 13.0, *) {
            ScrollView(.vertical, showsIndicators: true) {
                content
            }
            .scrollIndicators(.visible)
        } else {
            ScrollView(.vertical, showsIndicators: true) {
                content
            }
        }
    }
}

struct ContentView: View {
    enum Destination: String, CaseIterable, Identifiable {
        case runtime
        case dashboard
        case urlToPDF
        case pdfSqueezer
        case audioLab
        case library
        case models
        case logs
        case settings

        var id: String { rawValue }

        func title(_ lang: AppModel.UILanguage) -> String {
            switch self {
            case .runtime: return lang == .ja ? "実行環境" : "Runtime"
            case .dashboard: return lang == .ja ? "ダッシュボード" : "Dashboard"
            case .urlToPDF: return lang == .ja ? "URL → PDF 作成" : "URL to PDF"
            case .pdfSqueezer: return lang == .ja ? "PDF圧縮" : "PDF Squeezer"
            case .audioLab: return lang == .ja ? "オーディオブック変換" : "Audiobook"
            case .library: return lang == .ja ? "PDFライブラリ" : "PDF Library"
            case .models: return lang == .ja ? "AI接続" : "AI Connection"
            case .logs: return lang == .ja ? "ログ" : "Logs"
            case .settings: return lang == .ja ? "システム設定" : "Settings"
            }
        }

        var symbol: String {
            switch self {
            case .runtime: return "desktopcomputer"
            case .dashboard: return "square.grid.2x2"
            case .urlToPDF: return "doc.text.magnifyingglass"
            case .pdfSqueezer: return "arrow.down.right.and.arrow.up.left.circle"
            case .audioLab: return "waveform"
            case .library: return "books.vertical"
            case .models: return "brain"
            case .logs: return "terminal"
            case .settings: return "gearshape"
            }
        }
    }

    enum SettingsTab: String, CaseIterable, Identifiable {
        case general
        case runtime
        case models
        case logs

        var id: String { rawValue }

        func title(_ lang: AppModel.UILanguage) -> String {
            switch self {
            case .general: return lang == .ja ? "基本設定" : "General"
            case .runtime: return lang == .ja ? "実行環境" : "Runtime"
            case .models: return lang == .ja ? "AI接続" : "AI Connection"
            case .logs: return lang == .ja ? "ログ" : "Logs"
            }
        }
    }

    @ObservedObject var model: AppModel
    @State private var destination: Destination? = .dashboard
    @State private var settingsTab: SettingsTab = .general
    @State private var confirmClearRecentPDFs = false
    @FocusState private var isSourceURLFocused: Bool

    var body: some View {
        NavigationSplitView {
            List(selection: $destination) {
                Section(model.t("機能", "Features")) {
                    Label(Destination.dashboard.title(model.uiLanguage), systemImage: Destination.dashboard.symbol)
                        .tag(Destination.dashboard)
                    Label(Destination.urlToPDF.title(model.uiLanguage), systemImage: Destination.urlToPDF.symbol)
                        .tag(Destination.urlToPDF)
                    Label(Destination.audioLab.title(model.uiLanguage), systemImage: Destination.audioLab.symbol)
                        .tag(Destination.audioLab)
                    Label(Destination.pdfSqueezer.title(model.uiLanguage), systemImage: Destination.pdfSqueezer.symbol)
                        .tag(Destination.pdfSqueezer)
                }
                Section(model.t("システム", "System")) {
                    Label(Destination.settings.title(model.uiLanguage), systemImage: Destination.settings.symbol)
                        .tag(Destination.settings)
                }
            }
            .navigationTitle("WebTPDF Mac")
            .navigationSplitViewColumnWidth(min: 195, ideal: 215, max: 260)
        } detail: {
            Group {
                switch destination ?? .dashboard {
                case .runtime:
                    runtimeView
                case .dashboard:
                    dashboardView
                case .urlToPDF:
                    urlToPDFView
                case .pdfSqueezer:
                    pdfSqueezerView
                case .audioLab:
                    audioLabView
                case .library:
                    libraryView
                case .models:
                    modelsView
                case .logs:
                    systemLogsView
                case .settings:
                    settingsView
                }
            }
            .navigationTitle((destination ?? .dashboard).title(model.uiLanguage))
            .onAppear {
                if destination == nil {
                    destination = .dashboard
                }
            }
            .toolbar {
                ToolbarItemGroup {
                    Menu {
                        ForEach(AppModel.UILanguage.allCases) { lang in
                            Button {
                                model.uiLanguage = lang
                            } label: {
                                if model.uiLanguage == lang {
                                    Label(lang.label, systemImage: "checkmark")
                                } else {
                                    Text(lang.label)
                                }
                            }
                        }
                    } label: {
                        Text("  \(model.uiLanguage.label)  ")
                            .padding(.horizontal, 8)
                    }
                    .menuStyle(.borderlessButton)
                    .accessibilityLabel(model.t("言語", "Language"))

                    if let operation = model.activeOperationLabel {
                        HStack(spacing: 5) {
                            ProgressView()
                                .controlSize(.small)
                                .accessibilityHidden(true)
                            Text(operation)
                                .font(.callout)
                        }
                        .fixedSize()
                    }

                    if model.canCancelJob {
                        Button(role: .destructive) {
                            model.cancelJob()
                        } label: {
                            Label(model.t("停止", "Stop"), systemImage: "stop.fill")
                        }
                        .help(model.t("実行中の処理を停止", "Stop the current operation"))
                        .keyboardShortcut(".", modifiers: [.command])
                    }
                }
            }
        }
    }

    private var urlToPDFView: some View {
        VisibleScrollView {
            VStack(alignment: .leading, spacing: 24) {

                HStack(spacing: 10) {
                    let urlMissing = model.trimmedSourceURL.isEmpty
                    Button {
                        isSourceURLFocused = true
                    } label: {
                        topologyCard(
                            model.t("① ソースURL", "① Source URL"),
                            symbol: "globe",
                            detail: urlMissing ? model.t("未入力", "Empty") : model.t("入力済み", "Ready"),
                            state: urlMissing ? .needsInput : .ready
                        )
                    }
                    .buttonStyle(.plain)
                    .help(model.t("クリックしてURL入力欄にカーソルを移動", "Click to focus URL input"))

                    Image(systemName: "chevron.right").foregroundStyle(.secondary)

                    Button {
                        model.pageTurnDirection = model.pageTurnDirection == .left ? .right : .left
                    } label: {
                        topologyCard(
                            model.t("② ページ送り", "② Pagination"),
                            symbol: model.pageTurnDirection == .left ? "arrow.left.square" : "arrow.right.square",
                            detail: model.pageTurnDirection.label,
                            state: .ready
                        )
                    }
                    .buttonStyle(.plain)
                    .help(model.t("クリックして右キー・左キーを切り替え", "Click to toggle Left/Right key"))

                    Image(systemName: "chevron.right").foregroundStyle(.secondary)

                    Button {
                        model.choosePDFSaveDirectory()
                    } label: {
                        topologyCard(
                            model.t("③ 保存先", "③ Save To"),
                            symbol: "folder",
                            detail: model.fetchedPDFRoot.lastPathComponent,
                            state: .ready
                        )
                    }
                    .buttonStyle(.plain)
                    .help(model.t("クリックして保存先フォルダを変更", "Click to choose save folder"))

                    Image(systemName: "chevron.right").foregroundStyle(.secondary)

                    Button {
                        model.fetchPDFfromURL()
                    } label: {
                        topologyCard(
                            model.t("④ PDF保存 / キュー", "④ Save / Queue"),
                            symbol: "arrow.down.doc.fill",
                            detail: model.canFetchURL ? model.t("クリックで開始", "Click to Start") : model.t("①〜③を設定してください", "Complete ①–③"),
                            state: model.canFetchURL ? .actionReady : .disabled
                        )
                    }
                    .buttonStyle(.plain)
                    .disabled(!model.canFetchURL)
                    .help(model.t("クリックしてPDF保存を実行", "Click to create PDF"))
                }
                .padding(20)
                .frame(maxWidth: .infinity)
                .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 12))

                GroupBox(model.t("URL取得設定", "Capture Settings")) {
                    VStack(alignment: .leading, spacing: 14) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(model.t("① ソースURL（PDF / ページ送り対象サイト）", "① Source URL (PDF or paginated web viewer)"))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            TextField("https://... (PDF / URL)", text: $model.sourceURLText)
                                .textFieldStyle(.roundedBorder)
                                .focused($isSourceURLFocused)
                        }

                        HStack(spacing: 12) {
                            Text(model.t("② ページ送り方向", "② Page Turn Key"))
                                .font(.callout)
                                .foregroundStyle(.secondary)
                            Picker(model.t("② ページ送り方向", "② Page Turn Key"), selection: $model.pageTurnDirection) {
                                ForEach(AppModel.PageTurnDirection.allCases) { dir in
                                    Text(dir.label).tag(dir)
                                }
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .frame(maxWidth: 220)
                            Spacer()
                        }

                        HStack(spacing: 10) {
                            Text(model.t("③ 保存先フォルダ", "③ Save Folder"))
                                .font(.callout)
                                .foregroundStyle(.secondary)
                            Text(model.fetchedPDFRoot.path)
                                .font(.caption)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Button(model.t("変更", "Choose")) {
                                model.choosePDFSaveDirectory()
                            }
                            if model.customPDFSaveDirectory != nil {
                                Button(model.t("Desktopに戻す", "Reset")) {
                                    model.resetPDFSaveDirectory()
                                }
                            }
                            Spacer()
                        }

                        HStack(spacing: 10) {
                            Button(model.t("④ URLからPDF作成", "④ Create PDF")) {
                                model.fetchPDFfromURL()
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(!model.canFetchURL)

                            Button(model.t("④ ＋ キューに追加", "④ + Add to Queue")) {
                                model.enqueueCurrentURL()
                            }
                            .disabled(!model.canEnqueueURL)

                            if model.isRunning {
                                Button(model.t("キャンセル", "Cancel"), role: .destructive) {
                                    model.cancelJob()
                                }
                            }
                        }

                        HStack(spacing: 10) {
                            Button(model.t("専用Chromeを開く（事前ログイン）", "Open Dedicated Chrome (Sign In)")) {
                                model.launchLoginBrowser()
                            }
                            .disabled(model.isRunning)

                            Text(model.t("このアプリが自動操作するための専用Chromeを開きます。Kindle / O'Reilly などログインが必要なサイトは、ここから事前にログインしてください。", "Opens the dedicated Chrome browser controlled by this app. Sign in here beforehand for sites requiring authentication (e.g. Kindle / O'Reilly)."))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(8)
                }

                queueSection

                fetchHistorySection

                logBox(minHeight: 180)
            }
            .padding(28)
        }
    }

    private var queueSection: some View {
        GroupBox(model.t("実行キュー (\(model.pdfQueue.count)件)", "Execution Queue (\(model.pdfQueue.count))")) {
            VStack(alignment: .leading, spacing: 10) {
                if model.pdfQueue.isEmpty {
                    Text(model.t("URLとページ送り方向（← / →）を選んで「＋ キューに追加」を押すと、順番に自動でPDF化できます。", "Choose a URL and arrow direction (← / →), then click '+ Add to Queue' to process them sequentially."))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 4)
                } else {
                    ForEach($model.pdfQueue) { $item in
                        HStack(spacing: 10) {
                            Image(systemName: queueStatusSymbol(item.status))
                                .foregroundStyle(queueStatusColor(item.status))

                            Text(item.url)
                                .font(.caption)
                                .lineLimit(1)
                                .truncationMode(.middle)
                                .frame(maxWidth: .infinity, alignment: .leading)

                            Picker("", selection: $item.direction) {
                                ForEach(AppModel.PageTurnDirection.allCases) { dir in
                                    Text(dir.label).tag(dir)
                                }
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .frame(width: 150)
                            .disabled(item.status == .running)

                            if let out = item.outputURL {
                                Button(model.t("開く", "Open")) {
                                    NSWorkspace.shared.open(out)
                                }
                            }

                            if item.status == .failed || item.status == .completed {
                                Button {
                                    model.retryQueueItem(item.id)
                                } label: {
                                    Image(systemName: "arrow.clockwise")
                                }
                                .buttonStyle(.borderless)
                                .help(model.t("このURLを再実行", "Retry this URL"))
                            }

                            Button {
                                model.removeQueueItem(item.id)
                            } label: {
                                Image(systemName: "trash")
                            }
                            .buttonStyle(.borderless)
                            .disabled(item.status == .running)
                        }
                        if item.id != model.pdfQueue.last?.id {
                            Divider()
                        }
                    }

                    HStack(spacing: 10) {
                        if model.isQueueAutoPaused {
                            Button(model.t("自動実行を再開", "Resume Auto-Run")) {
                                model.resumeQueueAutoRun()
                            }
                            .buttonStyle(.borderedProminent)
                        } else {
                            Button(model.t("自動実行を一時停止", "Pause Auto-Run")) {
                                model.pauseQueueAutoRun()
                            }
                            .buttonStyle(.bordered)
                        }

                        Button(model.t("キューを連続実行", "Run Queue")) {
                            model.startQueue()
                        }
                        .disabled(!model.hasPendingQueueItems || model.isRunning)

                        Button(model.t("失敗分を再実行", "Retry Failed")) {
                            model.retryFailedQueueItems()
                        }
                        .disabled(!model.hasFailedQueueItems || model.isRunning)

                        Button(model.t("完了済みを整理", "Clear Finished")) {
                            model.clearFinishedQueueItems()
                        }
                        .disabled(model.isRunning)

                        Spacer()

                        Text(model.isQueueAutoPaused ? model.t("自動実行: 一時停止中", "Auto-Run: Paused") : model.t("自動実行: オン", "Auto-Run: ON"))
                            .font(.caption)
                            .foregroundStyle(model.isQueueAutoPaused ? .orange : .green)
                    }
                    .padding(.top, 4)
                }
            }
            .padding(8)
        }
    }

    private var fetchHistorySection: some View {
        let pageSize = 10
        let totalCount = model.urlFetchHistory.count
        let totalPages = max(1, (totalCount + pageSize - 1) / pageSize)
        let currentPage = min(max(0, model.historyPageIndex), totalPages - 1)
        let startIdx = currentPage * pageSize
        let endIdx = min(totalCount, startIdx + pageSize)
        let pageEntries = startIdx < endIdx ? Array(model.urlFetchHistory[startIdx..<endIdx]) : []

        return GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                if pageEntries.isEmpty {
                    Text(model.t("まだURL取得の履歴はありません。", "No URL fetch history yet."))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 4)
                } else {
                    ForEach(Array(pageEntries.enumerated()), id: \.element.id) { idx, entry in
                        HStack(spacing: 10) {
                            let queueStatus = model.activeHistoryQueueStatus(entry)
                            let isFetchingEntry = queueStatus == .running || (
                                model.isFetchingURL && !entry.succeeded
                                && (entry.errorMessage == "取得中..." || entry.errorMessage == "Fetching...")
                            )
                            if isFetchingEntry {
                                ProgressView()
                                    .controlSize(.small)
                                    .accessibilityLabel(model.t("取得中", "Fetching"))
                            } else if queueStatus == .pending {
                                Image(systemName: "clock")
                                    .accessibilityLabel(model.t("待機中", "Queued"))
                            } else {
                                Image(systemName: entry.hasWarning ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                                    .foregroundStyle(entry.hasWarning ? Color.orange : Color.green)
                            }

                            VStack(alignment: .leading, spacing: 2) {
                                Text(entry.url)
                                    .font(.caption)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                                HStack(spacing: 8) {
                                    Text(entry.direction.label)
                                    if let count = entry.pageCount {
                                        if let last = entry.lastPrintedPage, let total = entry.expectedPages {
                                            Text(model.t("PDF \(count)枚・Kindle \(last)/\(total)", "PDF \(count) pages · Kindle \(last)/\(total)"))
                                                .monospacedDigit()
                                        } else {
                                            let pages = entry.expectedPages.map { "\(count)/\($0)" }
                                                ?? (entry.isKindle ? model.t("\(count)/不明", "\(count)/unknown") : "\(count)")
                                            Text(model.t("\(pages) ページ", "\(pages) pages"))
                                                .monospacedDigit()
                                        }
                                    }
                                    if let path = entry.outputPath {
                                        Text(URL(fileURLWithPath: path).lastPathComponent)
                                            .lineLimit(1)
                                            .truncationMode(.middle)
                                    } else if let err = entry.errorMessage {
                                        Text(err)
                                            .help(err)
                                            .lineLimit(1)
                                            .truncationMode(.tail)
                                    }
                                }
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                if let verified = entry.verifiedPages {
                                    Text(model.t(
                                        "PDF最終確認済み \(verified)枚",
                                        "Final PDF verified · \(verified) pages"
                                    ))
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                }
                                if isFetchingEntry, let gaps = model.remainingKindleIntervals(for: entry) {
                                    Text(gaps == 0
                                         ? model.t("未確認 0 区間・PDF最終確認中…", "0 unchecked intervals · Final PDF verification…")
                                         : model.t("未確認 \(gaps) 区間・確認中…", "\(gaps) unchecked intervals · Checking…"))
                                        .font(.caption2)
                                        .monospacedDigit()
                                        .foregroundStyle(gaps > 0 ? Color.orange : Color.secondary)
                                } else if let gaps = entry.unverifiedGaps, gaps > 0 {
                                    Text(model.t(
                                        "最終ページ到達・未確認 \(gaps) 区間",
                                        "Last page reached · \(gaps) unchecked intervals"
                                    ))
                                    .font(.caption2)
                                    .foregroundStyle(.orange)
                                }
                                if entry.isKindle && entry.expectedPages == nil && entry.pageCount != nil {
                                    if let notice = model.historyPageCountMessages[entry.id] {
                                        Text(notice)
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
                                    }
                                    Button(model.checkingHistoryPageCount == entry.id
                                           ? model.t("確認中…", "Checking…")
                                           : model.t("総ページ数を確認", "Check total pages")) {
                                        model.checkKindleTotal(for: entry)
                                    }
                                    .buttonStyle(.link)
                                    .font(.caption2)
                                    .disabled(model.checkingHistoryPageCount != nil || model.isRunning)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)

                            if let path = entry.outputPath, FileManager.default.fileExists(atPath: path) {
                                let pdfURL = URL(fileURLWithPath: path)
                                Button(model.t("開く", "Open")) {
                                    NSWorkspace.shared.open(pdfURL)
                                }
                                Button(model.t("音声変換へ", "Convert to Audio")) {
                                    model.ebookURL = pdfURL
                                    destination = .audioLab
                                }
                            }

                            let retryButton = Button(
                                isFetchingEntry ? model.t("取得中…", "Fetching…")
                                : queueStatus == .pending ? model.t("待機中", "Queued")
                                : model.t("再ダウンロード", "Re-download")
                            ) {
                                model.redownloadFromHistory(entry)
                            }
                            .disabled(isFetchingEntry || queueStatus != nil)
                            if entry.hasWarning && !isFetchingEntry && queueStatus == nil {
                                retryButton
                                    .buttonStyle(.borderedProminent)
                                    .tint(.orange)
                            } else {
                                retryButton.buttonStyle(.bordered)
                            }

                            Button {
                                model.removeURLFetchHistoryItem(entry.id)
                            } label: {
                                Image(systemName: "trash")
                            }
                            .buttonStyle(.borderless)
                            .help(model.t("この履歴を削除", "Delete this history item"))
                        }
                        if idx < pageEntries.count - 1 {
                            Divider()
                        }
                    }

                    if totalPages > 1 {
                        Divider()
                        HStack {
                            Button(model.t("前へ", "Previous")) {
                                model.historyPageIndex = max(0, currentPage - 1)
                            }
                            .disabled(currentPage == 0)

                            Spacer()

                            Text("\(currentPage + 1) / \(totalPages)（\(startIdx + 1)〜\(endIdx)件 / 全\(totalCount)件）")
                                .font(.caption)
                                .foregroundStyle(.secondary)

                            Spacer()

                            Button(model.t("次へ", "Next")) {
                                model.historyPageIndex = min(totalPages - 1, currentPage + 1)
                            }
                            .disabled(currentPage >= totalPages - 1)
                        }
                        .padding(.top, 4)
                    }
                }
            }
            .padding(8)
        } label: {
            HStack {
                Text(model.t("取得履歴 (\(totalCount)件)", "Fetch History (\(totalCount))"))
                Spacer()
                Button(model.t("履歴を全消去", "Clear All History")) {
                    model.clearURLFetchHistory()
                }
                .disabled(model.urlFetchHistory.isEmpty)
            }
        }
    }

    private func queueStatusSymbol(_ status: AppModel.PDFQueueItem.Status) -> String {
        switch status {
        case .pending: return "clock"
        case .running: return "record.circle"
        case .completed: return "checkmark.circle.fill"
        case .failed: return "exclamationmark.triangle.fill"
        }
    }

    private func queueStatusColor(_ status: AppModel.PDFQueueItem.Status) -> Color {
        switch status {
        case .pending: return .secondary
        case .running: return .accentColor
        case .completed: return .green
        case .failed: return .orange
        }
    }

    private var audioLabView: some View {
        VisibleScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack(spacing: 10) {
                    let fileMissing = model.ebookURL == nil
                    Button {
                        model.chooseEbook()
                    } label: {
                        topologyCard(
                            model.t("① ファイル選択", "① Ebook File"),
                            symbol: "doc.text",
                            detail: model.ebookURL?.lastPathComponent ?? "PDF / EPUB / TXT",
                            state: fileMissing ? .needsInput : .ready
                        )
                    }
                    .buttonStyle(.plain)
                    .help(model.t("クリックして変換するファイルを選択", "Click to choose ebook file"))

                    Image(systemName: "chevron.right").foregroundStyle(.secondary)

                    let langMissing = model.language.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    Button {
                        let formats = AppModel.OutputFormat.allCases
                        if let idx = formats.firstIndex(of: model.outputFormat), !formats.isEmpty {
                            model.outputFormat = formats[(idx + 1) % formats.count]
                        }
                    } label: {
                        topologyCard(
                            model.t("② 形式・言語", "② Format"),
                            symbol: "music.note",
                            detail: "\(model.languageDisplayName(model.language)) · \(model.outputFormat.rawValue)",
                            state: langMissing ? .needsInput : .ready
                        )
                    }
                    .buttonStyle(.plain)
                    .help(model.t("クリックして出力形式（mp3 / m4b）を切り替え", "Click to toggle output format"))

                    Image(systemName: "chevron.right").foregroundStyle(.secondary)

                    let selectedModel = model.availableTTSModels.first(where: { $0.id == model.selectedTTSModelID })
                    let modelReady = selectedModel?.installed ?? false
                    Button {
                        let models = model.availableTTSModels
                        if let idx = models.firstIndex(where: { $0.id == model.selectedTTSModelID }), !models.isEmpty {
                            model.selectedTTSModelID = models[(idx + 1) % models.count].id
                        }
                    } label: {
                        let currentName = selectedModel?.name ?? model.selectedTTSModelID
                        topologyCard(
                            model.t("③ 音声モデル", "③ Voice Model"),
                            symbol: "waveform",
                            detail: currentName,
                            state: modelReady ? .ready : .needsInput
                        )
                    }
                    .buttonStyle(.plain)
                    .help(model.t("クリックして音声モデルを切り替え", "Click to cycle voice model"))

                    Image(systemName: "chevron.right").foregroundStyle(.secondary)

                    let canConvertAudio = model.canRun && !fileMissing && !langMissing && modelReady
                    Button {
                        model.startJob()
                    } label: {
                        topologyCard(
                            model.t("④ 音声変換開始", "④ Convert"),
                            symbol: "play.circle.fill",
                            detail: canConvertAudio ? model.t("クリックで開始", "Click to Start") : model.t("①〜③を設定してください", "Complete ①–③"),
                            state: canConvertAudio ? .actionReady : .disabled
                        )
                    }
                    .buttonStyle(.plain)
                    .disabled(!canConvertAudio)
                    .help(model.t("クリックして音声変換を開始", "Click to start audio conversion"))
                }
                .padding(20)
                .frame(maxWidth: .infinity)
                .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 12))

                inputGroup

                HStack {
                    Button(model.t("④ 音声変換を開始 (mp3/m4b)", "④ Start Audio Conversion (mp3/m4b)")) {
                        model.startJob()
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!model.canRun)

                    Button(model.t("キャンセル", "Cancel")) {
                        model.cancelJob()
                    }
                    .disabled(!model.isRunning)
                }

                logBox(minHeight: 160)
            }
            .padding(28)
        }
    }

    private var modelsView: some View {
        VisibleScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(model.t("AI接続の管理", "Manage AI Connections"))
                        .font(.title2.bold())
                    Text(model.t("音声変換 (Audio Lab) で使えるクラウドTTS API（OpenAI / ElevenLabs）と、テキスト整形・要約用のローカルLLM（Ollama）を管理します。", "Configure cloud TTS APIs (OpenAI / ElevenLabs) for Audio Lab and local LLMs (Ollama) for text preparation."))
                        .foregroundStyle(.secondary)
                }

                GroupBox(model.t("音声変換のおすすめ API 接続 (Audio Lab)", "Recommended TTS APIs for Audio Lab")) {
                    VStack(alignment: .leading, spacing: 14) {
                        Text(model.t("APIキーを設定すると、「音声変換 (Audio Lab)」の音声モデル一覧からクラウド高音質音声を直接選択できます。（※ Edge TTS は API キー不要・無料で利用できます）", "Once configured, cloud voices appear directly in Audio Lab's Voice Model picker. (Edge TTS works for free without an API key.)"))
                            .font(.caption)
                            .foregroundStyle(.secondary)

                        Divider()

                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                Label("OpenAI TTS (gpt-4o-mini-tts / tts-1)", systemImage: "waveform.circle")
                                    .font(.headline)
                                Spacer()
                                Text(model.openAIAPIKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? model.t("未設定", "Not Set") : model.t("設定済み", "Configured"))
                                    .font(.caption)
                                    .foregroundStyle(model.openAIAPIKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Color.secondary : Color.green)
                            }
                            HStack(spacing: 10) {
                                SecureField("OPENAI_API_KEY (sk-...)", text: $model.openAIAPIKey)
                                    .textFieldStyle(.roundedBorder)
                                Picker(model.t("声質", "Voice"), selection: $model.openAITTSVoice) {
                                    ForEach(["coral", "alloy", "nova", "sage", "shimmer", "onyx"], id: \.self) { v in
                                        Text(v).tag(v)
                                    }
                                }
                                .frame(width: 130)
                            }
                        }

                        Divider()

                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                Label("ElevenLabs (eleven_multilingual_v2)", systemImage: "sparkles")
                                    .font(.headline)
                                Spacer()
                                Text(model.elevenLabsAPIKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? model.t("未設定", "Not Set") : model.t("設定済み", "Configured"))
                                    .font(.caption)
                                    .foregroundStyle(model.elevenLabsAPIKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Color.secondary : Color.green)
                            }
                            HStack(spacing: 10) {
                                SecureField("ELEVENLABS_API_KEY", text: $model.elevenLabsAPIKey)
                                    .textFieldStyle(.roundedBorder)
                                TextField("Voice ID", text: $model.elevenLabsVoiceID)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 210)
                            }
                        }
                    }
                    .padding(8)
                }

                GroupBox("ローカル LLM (Ollama)") {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(model.t("※ 音声読み上げ自体は macOS say / Edge TTS / クラウドTTS API で動作するため、Ollama は未設定でも音声変換できます（今後のテキスト整形・要約用）。", "Note: Audio Lab uses macOS say, Edge TTS, or cloud TTS APIs for speech synthesis; Ollama is optional (for future text prep/summarization)."))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Ollama")
                                    .font(.headline)
                                Text(model.ollamaStatusMessage)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button("再読み込み", systemImage: "arrow.clockwise") {
                                model.refreshOllamaModels()
                            }
                            .disabled(model.isRefreshingOllama)
                        }

                        if model.isRefreshingOllama {
                            ProgressView("読み込み中...").controlSize(.small)
                        } else if !model.ollamaModels.isEmpty {
                            Picker("利用モデル", selection: $model.selectedOllamaModelName) {
                                ForEach(model.ollamaModels) { item in
                                    Text(item.name).tag(item.name)
                                }
                            }
                            if let selected = model.ollamaModels.first(where: { $0.name == model.selectedOllamaModelName }) {
                                GroupBox {
                                    VStack(alignment: .leading, spacing: 8) {
                                        if let family = selected.details?.family {
                                            LabeledContent("ファミリー", value: family)
                                        }
                                        if let params = selected.details?.parameter_size {
                                            LabeledContent("パラメータ数", value: params)
                                        }
                                        if let quant = selected.details?.quantization_level {
                                            LabeledContent("量子化", value: quant)
                                        }
                                        if let size = selected.size {
                                            LabeledContent("サイズ", value: ByteCountFormatter.string(fromByteCount: size, countStyle: .file))
                                        }
                                        HStack(spacing: 10) {
                                            Button("推論テスト", systemImage: "waveform.path.ecg") {
                                                model.testOllamaInference()
                                            }
                                            .disabled(model.isTestingOllama)
                                            if model.isTestingOllama {
                                                ProgressView().controlSize(.small)
                                            } else if !model.ollamaTestOutput.isEmpty {
                                                Text(model.ollamaTestOutput)
                                                    .font(.caption)
                                                    .foregroundStyle(.secondary)
                                            }
                                        }
                                    }
                                    .padding(4)
                                }
                            }
                        }

                        Divider()

                        HStack(spacing: 8) {
                            TextField("モデル名 (例: gemma3:4b, qwen2.5:7b)", text: $model.ollamaPullName)
                                .textFieldStyle(.roundedBorder)
                            Button("ダウンロード (pull)") {
                                model.pullOllamaModel(model.ollamaPullName)
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(model.isRunning || model.ollamaPullName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        }
                    }
                    .padding(8)
                }

                logBox(minHeight: 160)
            }
            .padding(28)
        }
    }

    private var runtimeView: some View {
        VisibleScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("実行環境の診断")
                            .font(.title2.bold())
                        Spacer()
                        Text(model.t("現在バージョン: \(model.appVersionDisplay)", "Version: \(model.appVersionDisplay)"))
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                    }
                    Text("URLからのPDF取得・ローカル音声変換・AI連携に必要なコンポーネントの状態を確認します。")
                        .foregroundStyle(.secondary)
                    if let prev = UserDefaults.standard.string(forKey: "lastUpdatedFromVersion"), !prev.isEmpty, prev != model.appVersionDisplay {
                        Text(model.t("前回のアップデート: \(prev) → \(model.appVersionDisplay)", "Last update: \(prev) → \(model.appVersionDisplay)"))
                            .font(.caption)
                            .foregroundStyle(.green)
                    }
                }

                GroupBox("ランタイム診断") {
                    VStack(spacing: 0) {
                        ForEach(Array(model.runtimeChecks.enumerated()), id: \.element.id) { idx, check in
                            HStack(spacing: 10) {
                                Label(
                                    check.name,
                                    systemImage: check.ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
                                )
                                .foregroundStyle(check.ok ? .primary : .secondary)
                                Spacer()
                                Text(check.detail)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                                if let actionTitle = check.actionTitle {
                                    Button(actionTitle) {
                                        model.runRuntimeAction(check)
                                    }
                                    .disabled(model.isRunning)
                                }
                            }
                            .padding(.vertical, 8)
                            .padding(.horizontal, 4)
                            if idx < model.runtimeChecks.count - 1 {
                                Divider()
                            }
                        }
                    }
                }

                HStack(spacing: 10) {
                    Button(model.t("再診断", "Re-check"), systemImage: "arrow.clockwise") {
                        model.refreshOllamaModels()
                        model.refreshRuntimeChecks()
                    }

                    Button(model.t("必要ツールを一括更新", "Update Dependencies"), systemImage: "shippingbox") {
                        model.installOrUpdateAllDependencies()
                    }
                    .disabled(model.isRunning)

                    updateAppButton
                        .buttonStyle(.borderedProminent)
                }

                logBox(minHeight: 160)
            }
            .padding(28)
        }
    }

    private var updateAppButton: some View {
        HStack(spacing: 12) {
            VStack(alignment: .trailing, spacing: 3) {
                Text(model.t("現在のバージョン \(model.appVersionDisplay)", "Installed version \(model.appVersionDisplay)"))
                    .monospacedDigit()
                if let checkedAt = model.lastUpdateCheckAt {
                    let date = checkedAt.formatted(.dateTime.year().month().day().hour().minute())
                    Text(model.t("最終確認 \(date)", "Last checked \(date)"))
                } else {
                    Text(model.t("最終確認 未確認", "Last checked: Never"))
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            Button {
                model.updateAndRelaunchApp()
            } label: {
                HStack(spacing: 6) {
                    if model.isUpdatingApp {
                        ProgressView()
                            .controlSize(.small)
                    } else {
                        Image(systemName: "arrow.down.circle")
                    }
                    Text(
                        model.isUpdatingApp
                            ? model.t("アップデート中...", "Updating...")
                            : model.t("アプリをアップデート", "Update App")
                    )
                }
            }
            .disabled(model.isRunning)
        }
    }

    private var dashboardView: some View {
        VisibleScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack(spacing: 12) {
                    Button("URL → PDF 作成を開く") { destination = .urlToPDF }
                        .buttonStyle(.borderedProminent)
                    Button("音声変換を開く") { destination = .audioLab }
                    Button(model.t("システム設定を開く", "Open Settings")) {
                        settingsTab = .general
                        destination = .settings
                    }
                    Spacer()
                    updateAppButton
                }

                dashboardRuntimeBox

                fetchHistorySection

                logBox(minHeight: 160)
            }
            .padding(28)
        }
    }

    private var dashboardRuntimeBox: some View {
        let checks = model.runtimeChecks
        let missing = checks.filter { !$0.ok }
        let allReady = !checks.isEmpty && missing.isEmpty

        return GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) {
                    Image(systemName: allReady ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                        .foregroundStyle(allReady ? .green : .orange)
                    Text(
                        allReady
                            ? model.t("必要な実行環境はすべて揃っています。", "All required runtime tools are ready.")
                            : model.t("未整備の実行環境が \(missing.count) 件あります。実行環境画面から導入・起動してください。", "\(missing.count) runtime item(s) need setup. Open Runtime to install or launch them.")
                    )
                    .font(.callout)
                    Spacer()
                    if !allReady {
                        Button(model.t("実行環境を開いて整備する", "Open Runtime Setup")) {
                            destination = .runtime
                        }
                        .buttonStyle(.borderedProminent)
                    } else {
                        Button(model.t("実行環境を確認", "View Runtime")) {
                            destination = .runtime
                        }
                    }
                }

                if !checks.isEmpty {
                    Divider()
                    VStack(spacing: 6) {
                        ForEach(checks) { item in
                            HStack(spacing: 8) {
                                Image(systemName: item.ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                                    .foregroundStyle(item.ok ? .green : .orange)
                                Text(item.name)
                                    .font(.caption.weight(.medium))
                                Spacer()
                                Text(item.detail)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                            }
                        }
                    }
                }
            }
            .padding(8)
        } label: {
            Text(model.t("実行環境の準備状況", "Runtime Environment"))
        }
    }

    private var pdfSqueezerView: some View {
        VisibleScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(model.t("PDF圧縮 (Squeezer)", "PDF Squeezer"))
                        .font(.title2.bold())
                    Text(model.t("PDF内の画像・ストリームを最適化してファイルサイズを大幅に軽量化します。フォルダごと一括圧縮にも対応しています。", "Optimize embedded images and streams to shrink PDF file sizes. Supports single PDFs or batch folder compression."))
                        .foregroundStyle(.secondary)
                }

                HStack(spacing: 10) {
                    let targetMissing = model.squeezerTargetURL == nil
                    Button {
                        model.chooseSqueezerPDF()
                    } label: {
                        topologyCard(
                            model.t("① 対象PDF", "① Target"),
                            symbol: "doc.fill",
                            detail: model.squeezerTargetURL?.lastPathComponent ?? model.t("未選択", "Not selected"),
                            state: targetMissing ? .needsInput : .ready
                        )
                    }
                    .buttonStyle(.plain)
                    .help(model.t("クリックして圧縮するPDFを選択", "Click to choose target PDF"))

                    Image(systemName: "chevron.right").foregroundStyle(.secondary)

                    Button {
                        let presets = AppModel.PDFCompressionPreset.allCases
                        if let idx = presets.firstIndex(of: model.squeezerPreset), !presets.isEmpty {
                            model.squeezerPreset = presets[(idx + 1) % presets.count]
                        }
                    } label: {
                        topologyCard(
                            model.t("② 圧縮レベル", "② Preset"),
                            symbol: "slider.horizontal.3",
                            detail: model.squeezerPreset.label(model.uiLanguage),
                            state: .ready
                        )
                    }
                    .buttonStyle(.plain)
                    .help(model.t("クリックして圧縮レベルを切り替え", "Click to cycle compression preset"))

                    Image(systemName: "chevron.right").foregroundStyle(.secondary)

                    Button {
                        model.squeezerInPlace.toggle()
                    } label: {
                        topologyCard(
                            model.t("③ 保存方法", "③ Output Mode"),
                            symbol: "arrow.triangle.2.circlepath",
                            detail: model.squeezerInPlace ? model.t("上書き保存", "Overwrite") : "_compressed.pdf",
                            state: .ready
                        )
                    }
                    .buttonStyle(.plain)
                    .help(model.t("クリックして上書き／別名保存を切り替え", "Click to toggle overwrite or _compressed.pdf"))

                    Image(systemName: "chevron.right").foregroundStyle(.secondary)

                    let canCompress = !targetMissing && !model.isRunning
                    Button {
                        model.runPDFSqueezer()
                    } label: {
                        topologyCard(
                            model.t("④ PDFを圧縮", "④ Compress"),
                            symbol: "arrow.down.right.and.arrow.up.left.circle.fill",
                            detail: canCompress ? model.t("クリックで開始", "Click to Start") : model.t("①〜③を設定してください", "Complete ①–③"),
                            state: canCompress ? .actionReady : .disabled
                        )
                    }
                    .buttonStyle(.plain)
                    .disabled(!canCompress)
                    .help(model.t("クリックしてPDF圧縮を開始", "Click to compress PDF"))
                }
                .padding(20)
                .frame(maxWidth: .infinity)
                .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 12))

                GroupBox(model.t("圧縮設定", "Compression Settings")) {
                    VStack(alignment: .leading, spacing: 14) {
                        HStack(spacing: 10) {
                            Text(model.t("① 対象", "① Target"))
                                .font(.callout)
                                .foregroundStyle(.secondary)
                            Text(model.squeezerTargetURL?.path ?? model.t("未選択", "Not selected"))
                                .font(.caption)
                                .lineLimit(1)
                                .truncationMode(.middle)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Button(model.t("PDFを選択", "Choose PDF")) {
                                model.chooseSqueezerPDF()
                            }
                            .disabled(model.isRunning)
                            Button(model.t("フォルダ一括", "Choose Folder")) {
                                model.chooseSqueezerFolder()
                            }
                            .disabled(model.isRunning)
                        }

                        HStack(spacing: 12) {
                            Text(model.t("② 圧縮プリセット", "② Preset"))
                                .font(.callout)
                                .foregroundStyle(.secondary)
                            Picker(model.t("② 圧縮プリセット", "② Preset"), selection: $model.squeezerPreset) {
                                ForEach(AppModel.PDFCompressionPreset.allCases) { preset in
                                    Text(preset.label(model.uiLanguage)).tag(preset)
                                }
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                        }

                        Toggle(
                            model.t("③ 元のファイルを置き換える（オフの場合は _compressed.pdf を作成）", "③ Overwrite original file(s) (creates _compressed.pdf when off)"),
                            isOn: $model.squeezerInPlace
                        )
                        .disabled(model.isRunning)

                        HStack(spacing: 10) {
                            Button(model.t("④ PDFを圧縮", "④ Compress PDF(s)")) {
                                model.runPDFSqueezer()
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(model.squeezerTargetURL == nil || model.isRunning)

                            if model.isRunning {
                                Button(model.t("キャンセル", "Cancel"), role: .destructive) {
                                    model.cancelJob()
                                }
                            }
                        }
                    }
                    .padding(8)
                }

                logBox(minHeight: 220)
            }
            .padding(28)
        }
    }

    private var libraryView: some View {
        VisibleScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(model.t("PDFライブラリ", "PDF Library"))
                            .font(.title2.bold())
                        Text(model.t("選択したフォルダーのPDFを開いたり、音声変換に送ったりできます。", "Open PDFs in the selected folder or send them to Audio Lab."))
                            .foregroundStyle(.secondary)
                        Label(model.libraryFolder.path, systemImage: "folder")
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .help(model.libraryFolder.path)
                    }
                    Spacer()
                    Button(model.t("フォルダーを選択", "Choose Folder"), systemImage: "folder") {
                        model.chooseLibraryFolder()
                    }
                    Button(model.t("再読み込み", "Reload"), systemImage: "arrow.clockwise") {
                        model.refreshLibraryPDFs()
                    }
                }

                GroupBox(model.t("PDF一覧 (\(model.libraryPDFs.count)件)", "PDFs (\(model.libraryPDFs.count))")) {
                    if let error = model.libraryError {
                        Text(model.t("フォルダーを読み込めませんでした。\(error)", "Could not read the folder. \(error)"))
                            .foregroundStyle(.secondary)
                            .padding(12)
                    } else if model.libraryPDFs.isEmpty {
                        Text(model.t("選択したフォルダーにPDFがありません。", "The selected folder contains no PDFs."))
                            .foregroundStyle(.secondary)
                            .padding(12)
                    } else {
                        VStack(spacing: 0) {
                            ForEach(Array(model.libraryPDFs.enumerated()), id: \.element) { idx, pdf in
                                HStack(spacing: 10) {
                                    Label(pdf.lastPathComponent, systemImage: "doc.fill")
                                        .lineLimit(1)
                                        .truncationMode(.middle)
                                    Spacer()
                                    if let historyEntry = model.historyItem(forPDF: pdf) {
                                        Button(model.t("再ダウンロード", "Re-download")) {
                                            model.redownloadFromHistory(historyEntry)
                                        }
                                    }
                                    Button(model.t("開く", "Open")) { NSWorkspace.shared.open(pdf) }
                                    Button("Finder") {
                                        NSWorkspace.shared.activateFileViewerSelecting([pdf])
                                    }
                                    Button(model.t("音声変換へ", "Convert to Audio")) {
                                        model.ebookURL = pdf
                                        destination = .audioLab
                                    }
                                    .buttonStyle(.borderedProminent)
                                }
                                .padding(.vertical, 8)
                                if idx < model.libraryPDFs.count - 1 {
                                    Divider()
                                }
                            }
                        }
                    }
                }
            }
            .padding(28)
        }
    }

    private var settingsView: some View {
        VStack(spacing: 0) {
            Picker("", selection: $settingsTab) {
                ForEach(SettingsTab.allCases) { tab in
                    Text(tab.title(model.uiLanguage)).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 28)
            .padding(.top, 20)
            .padding(.bottom, 8)

            Divider()

            switch settingsTab {
            case .general:
                VisibleScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("システム設定")
                                .font(.title2.bold())
                            Text(model.t("PDF取得時のページ送り上限を設定します。", "Set the maximum number of page turns for PDF capture."))
                                .foregroundStyle(.secondary)
                        }

                        GroupBox(model.t("PDF取得設定", "PDF Capture Settings")) {
                            HStack {
                                Text(model.t("最大ページ送り数", "Maximum Page Turns"))
                                Spacer()
                                TextField("1000", text: $model.maxPagesSetting)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 100)
                            }
                            .padding(8)
                        }
                    }
                    .padding(28)
                }
            case .runtime:
                runtimeView
            case .models:
                modelsView
            case .logs:
                systemLogsView
            }
        }
    }

    private var systemLogsView: some View {
        VisibleScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("環境・実行ログ")
                        .font(.title2.bold())
                    Text("直近の実行ログと出力先を確認します。")
                        .foregroundStyle(.secondary)
                }

                GroupBox("環境ステータス") {
                    VStack(spacing: 0) {
                        StatusRow(title: "取得済みPDF", value: model.fetchedPDFURL?.path ?? "-", symbol: "doc.fill")
                        Divider()
                        StatusRow(title: "出力フォルダ", value: model.outputDirectory?.path ?? "-", symbol: "folder")
                    }
                }

                HStack {
                    Button("ログをコピー", systemImage: "doc.on.doc") {
                        model.copyLogToPasteboard()
                    }
                    Button("ログをクリア", systemImage: "trash") {
                        model.clearLog()
                    }
                }

                logBox(minHeight: 360)
            }
            .padding(28)
        }
    }

    private enum CardStepState {
        case needsInput
        case ready
        case actionReady
        case disabled
    }

    private func topologyCard(_ title: String, symbol: String, detail: String, state: CardStepState = .ready) -> some View {
        let iconColor: Color = {
            switch state {
            case .needsInput: return .orange
            case .ready: return .green
            case .actionReady: return .accentColor
            case .disabled: return .secondary
            }
        }()
        let bgColor: Color = {
            switch state {
            case .needsInput: return Color.orange.opacity(0.14)
            case .ready: return Color.green.opacity(0.08)
            case .actionReady: return Color.accentColor.opacity(0.18)
            case .disabled: return Color.secondary.opacity(0.10)
            }
        }()
        let borderColor: Color = {
            switch state {
            case .needsInput: return Color.orange.opacity(0.75)
            case .ready: return Color.green.opacity(0.45)
            case .actionReady: return Color.accentColor.opacity(0.85)
            case .disabled: return Color.secondary.opacity(0.25)
            }
        }()
        let borderWidth: CGFloat = (state == .needsInput || state == .actionReady) ? 1.5 : 1.0

        return VStack(spacing: 6) {
            Image(systemName: symbol)
                .font(.title2)
                .foregroundStyle(iconColor)
            Text(title)
                .font(.headline)
                .foregroundStyle(state == .needsInput ? Color.orange : (state == .disabled ? Color.secondary : Color.primary))
            Text(detail)
                .font(.caption)
                .foregroundStyle(state == .needsInput ? Color.orange.opacity(0.9) : Color.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 12)
        .padding(.horizontal, 10)
        .background(bgColor, in: RoundedRectangle(cornerRadius: 10))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(borderColor, lineWidth: borderWidth)
        )
        .opacity(state == .disabled ? 0.5 : 1.0)
        .contentShape(RoundedRectangle(cornerRadius: 10))
        .onHover { hovering in
            if hovering && state != .disabled {
                NSCursor.pointingHand.push()
            } else {
                NSCursor.pop()
            }
        }
    }

    private func logBox(minHeight: CGFloat) -> some View {
        GroupBox(model.t("ログ", "Log")) {
            ScrollViewReader { proxy in
                VisibleScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        Text(model.logText)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                        Color.clear
                            .frame(height: 1)
                            .id("log-bottom")
                    }
                }
                .frame(height: minHeight)
                .onChange(of: model.logText) { _, _ in
                    proxy.scrollTo("log-bottom", anchor: .bottom)
                }
            }
        }
    }

    private var inputGroup: some View {
        GroupBox(model.t("入力", "Input")) {
            VStack(alignment: .leading, spacing: 12) {
                FilePickerRow(
                    title: model.t("① 電子書籍ファイル", "① Ebook File"),
                    url: model.ebookURL,
                    buttonTitle: model.t("選択", "Choose"),
                    action: { model.chooseEbook() },
                    clearAction: model.ebookURL == nil ? nil : { model.clearEbook() }
                )

                languageAndFormatRow

                localModelSection

                sampleOptions

                Toggle(
                    model.t(
                        "ドライラン（音声ファイルは作らず、設定や出力先のテストのみ実行）",
                        "Dry-run (test settings and output path without generating audio)"
                    ),
                    isOn: $model.dryRun
                )
            }
            .padding(8)
        }
    }

    private var localModelSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Picker("③ 音声モデル", selection: $model.selectedTTSModelID) {
                    ForEach(model.availableTTSModels) { item in
                        Text(item.installed ? "\(item.name) [済]" : "\(item.name) [未DL]")
                            .tag(item.id)
                    }
                }

                Button("モデルDL") {
                    model.downloadModelTarget(model.selectedTTSModelID)
                }
                .disabled(model.isRunning)
            }

            HStack(spacing: 8) {
                TextField("新しいモデルURL (https://... .zip / .onnx 等)", text: $model.customModelURLText)
                    .textFieldStyle(.roundedBorder)

                Button("URLからモデルDL") {
                    model.downloadModelTarget(model.customModelURLText)
                }
                .disabled(model.isRunning || model.customModelURLText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            if let selected = model.availableTTSModels.first(where: { $0.id == model.selectedTTSModelID }) {
                Text(model.t("エンジン: \(selected.engine) · 言語: \(selected.language)", "Engine: \(selected.engine) · Language: \(selected.language)"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .background(Color(nsColor: .controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var languageAndFormatRow: some View {
        HStack(spacing: 16) {
            Picker(model.t("② 言語", "② Language"), selection: $model.language) {
                ForEach(model.availableLanguagesForSelectedModel, id: \.self) { code in
                    Text(model.languageDisplayName(code)).tag(code)
                }
            }
            .onChange(of: model.selectedTTSModelID) { _, _ in
                model.syncLanguageForSelectedModel()
            }

            Picker(model.t("出力形式", "Format"), selection: $model.outputFormat) {
                ForEach(AppModel.OutputFormat.allCases) { format in
                    Text(format.rawValue).tag(format)
                }
            }

            Spacer()
        }
    }

    private var sampleOptions: some View {
        VStack(alignment: .leading, spacing: 8) {
            Toggle("短い範囲だけ試す（PDF先頭ページ）", isOn: $model.sampleMode)

            HStack(spacing: 8) {
                Text("先頭")
                    .foregroundStyle(.secondary)
                TextField("8", text: $model.samplePageCount)
                    .frame(width: 70)
                    .disabled(!model.sampleMode)
                Text("ページだけ変換")
                    .foregroundStyle(.secondary)
            }

            Text("ONの場合、元PDFは変更せず、一時PDFを出力フォルダ内に作って変換します。")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color(nsColor: .controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct StatusRow: View {
    let title: String
    let value: String
    let symbol: String

    var body: some View {
        HStack {
            Label(title, systemImage: symbol)
            Spacer()
            Text(value)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 4)
    }
}

struct FilePickerRow: View {
    let title: String
    let url: URL?
    let buttonTitle: String
    let action: () -> Void
    var placeholder: String = "PDF / EPUB / TXT"
    var clearAction: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
            HStack {
                Button(buttonTitle, action: action)
                Text(url?.path ?? placeholder)
                    .font(url == nil ? .caption : .body)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .foregroundStyle(url == nil ? .secondary : .primary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if let clearAction {
                    Button("クリア", action: clearAction)
                }
            }
        }
    }
}

struct KeyValueRow: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .lineLimit(2)
                .truncationMode(.middle)
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color(nsColor: .controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}
