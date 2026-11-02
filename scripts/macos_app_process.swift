#!/usr/bin/env swift
// The updater controls only the desktop application, never the daemon or workloads.
import AppKit
import Foundation

private let bundleIdentifiers: Set<String> = [
    "local.serverpilot.desktop", "local.gpu-broker.desktop",
]
private let timeoutSeconds: TimeInterval = 15

private enum UpdateError: Error, CustomStringConvertible {
    case message(String)

    var description: String {
        switch self {
        case .message(let text): return text
        }
    }
}

@MainActor
private final class LaunchResult {
    var app: NSRunningApplication?
    var error: Error?
}

private func normalizedPath(_ url: URL) -> String {
    url.standardizedFileURL.resolvingSymlinksInPath().path
}

@MainActor
private func runningApplications() -> [NSRunningApplication] {
    NSWorkspace.shared.runningApplications.filter {
        !$0.isTerminated && bundleIdentifiers.contains($0.bundleIdentifier ?? "")
    }.sorted { $0.processIdentifier < $1.processIdentifier }
}

@MainActor
private func describe(_ app: NSRunningApplication) -> [String: Any] {
    var result: [String: Any] = [
        "pid": Int(app.processIdentifier),
        "finished_launching": app.isFinishedLaunching,
    ]
    if let url = app.bundleURL {
        result["bundle_path"] = normalizedPath(url)
        // These fields describe the current bundle on disk, not an old image
        // already loaded in the process. The updater also requires a fresh PID.
        if let bundle = Bundle(url: url) {
            result["bundle_version"] = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString")
            result["bundle_build_id"] = bundle.object(forInfoDictionaryKey: "ServerPilotBuildID")
            result["bundle_backend_build_id"] = bundle.object(forInfoDictionaryKey: "ServerPilotBackendBuildID")
        }
    }
    if let url = app.executableURL {
        result["executable_path"] = normalizedPath(url)
    }
    if let date = app.launchDate {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        result["launch_time"] = formatter.string(from: date)
    }
    return result
}

@MainActor
private func instances() -> [[String: Any]] {
    runningApplications().map(describe)
}

@MainActor
private func waitUntil(_ condition: () -> Bool) -> Bool {
    let deadline = ProcessInfo.processInfo.systemUptime + timeoutSeconds
    repeat {
        if condition() { return true }
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
    } while ProcessInfo.processInfo.systemUptime < deadline
    return condition()
}

@MainActor
private func quitApplications() throws -> [String: Any] {
    let apps = runningApplications()
    for app in apps {
        guard app.terminate() else {
            throw UpdateError.message("ServerPilot PID \(app.processIdentifier) refused to quit; update stopped.")
        }
    }
    guard waitUntil({ runningApplications().isEmpty }) else {
        let pids = runningApplications().map { String($0.processIdentifier) }.joined(separator: ", ")
        throw UpdateError.message("ServerPilot did not quit within \(Int(timeoutSeconds)) seconds (PIDs: \(pids)); update stopped.")
    }
    return ["quit_pids": apps.map { Int($0.processIdentifier) }, "instances": instances()]
}

@MainActor
private func validatedApplicationURL(for path: String) throws -> URL {
    guard path.hasPrefix("/") else {
        throw UpdateError.message("The app path must be absolute.")
    }
    let url = URL(fileURLWithPath: path, isDirectory: true).standardizedFileURL.resolvingSymlinksInPath()
    guard url.lastPathComponent == "ServerPilot.app",
          let bundle = Bundle(url: url),
          bundle.bundleIdentifier == "local.serverpilot.desktop",
          let executable = bundle.executableURL,
          FileManager.default.isExecutableFile(atPath: executable.path) else {
        throw UpdateError.message("Refusing to launch anything except the canonical ServerPilot.app bundle.")
    }
    return url
}

@MainActor
private func launchApplication(at path: String) throws -> [String: Any] {
    let url = try validatedApplicationURL(for: path)
    guard runningApplications().isEmpty else {
        throw UpdateError.message("A ServerPilot app is still running; quit it before launching the updated bundle.")
    }

    let result = LaunchResult()
    let configuration = NSWorkspace.OpenConfiguration()
    configuration.activates = true
    configuration.createsNewApplicationInstance = false
    // The parent updater removes fixture overrides and selects the canonical
    // CLI before invoking us. Do not fall back to launchd's stale GUI environment.
    configuration.environment = ProcessInfo.processInfo.environment
    NSWorkspace.shared.openApplication(at: url, configuration: configuration) { application, error in
        Task { @MainActor in
            result.app = application
            result.error = error
        }
    }
    guard waitUntil({ result.app != nil || result.error != nil }) else {
        throw UpdateError.message("ServerPilot launch did not complete within \(Int(timeoutSeconds)) seconds.")
    }
    if let launchError = result.error { throw launchError }
    guard let opened = result.app, waitUntil({ opened.isFinishedLaunching || opened.isTerminated }),
          !opened.isTerminated else {
        throw UpdateError.message("ServerPilot exited or did not finish launching.")
    }
    let apps = runningApplications()
    guard apps.count == 1,
          apps[0].processIdentifier == opened.processIdentifier,
          apps[0].bundleURL.map(normalizedPath) == url.path else {
        throw UpdateError.message("The launched app is not the sole ServerPilot instance at the requested path.")
    }
    return ["launched_pid": Int(opened.processIdentifier), "instances": apps.map(describe)]
}

@MainActor
private func restartApplication(at path: String) throws -> [String: Any] {
    // A wrong destination must not close a user's current window. Validate
    // before quitting, then keep final quit and launch in this one invocation.
    _ = try validatedApplicationURL(for: path)
    let closed = try quitApplications()
    var launched = try launchApplication(at: path)
    launched["quit_pids"] = closed["quit_pids"]
    return launched
}

@MainActor
private func run() throws -> [String: Any] {
    let arguments = Array(CommandLine.arguments.dropFirst())
    switch arguments.first {
    case "inspect" where arguments.count == 1:
        return ["instances": instances()]
    case "quit" where arguments.count == 1:
        return try quitApplications()
    case "launch" where arguments.count == 2:
        return try launchApplication(at: arguments[1])
    case "restart" where arguments.count == 2:
        return try restartApplication(at: arguments[1])
    case "--help", "-h":
        return ["usage": "macos_app_process.swift inspect | quit | launch /absolute/path/ServerPilot.app | restart /absolute/path/ServerPilot.app"]
    default:
        throw UpdateError.message("Usage: macos_app_process.swift inspect | quit | launch /absolute/path/ServerPilot.app | restart /absolute/path/ServerPilot.app")
    }
}

MainActor.assumeIsolated {
    do {
        let data = try JSONSerialization.data(withJSONObject: run(), options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    } catch {
        FileHandle.standardError.write(Data("\(error)\n".utf8))
        exit(1)
    }
}
