// Generated from src/serverpilot/agent_contract.py. Do not edit.
// `serverpilot mcp policy --check` fails if this drifts from it.

public enum CardVocabulary {
    public static let labelByState: [String: String] = [
        "AVAILABLE": "空闲",
        "KEEPALIVE": "空闲",
        "RUNNING_MANAGED": "运行中",
        "HELD": "已分配未运行",
        "LEASED_IDLE": "已分配未运行",
        "BUSY_UNMANAGED": "外部进程",
        "CONFLICT": "归属冲突",
        "ORPHANED_BUSY": "归属冲突",
        "UNKNOWN_STALE": "数据过期",
        "UNKNOWN_RECOVERING": "数据过期",
        "UNHEALTHY": "异常",
        "ERROR": "异常",
        "MAINTENANCE": "异常",
        "SCHEDULER_MANAGED": "调度器管理",
    ]

    public static func label(forState state: String) -> String {
        labelByState[state] ?? "数据过期"
    }
}
