import XCTest

@MainActor
final class FixtureModeUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchEnvironment["SERVERPILOT_DESKTOP_FIXTURE"] = "1"
        app.launchEnvironment["SERVERPILOT_CLI"] = "/usr/bin/false"
        app.launchArguments += ["-ApplePersistenceIgnoreState", "YES"]
        app.launch()
    }

    override func tearDownWithError() throws {
        app.terminate()
        app = nil
    }

    func testFixtureModeLaunchesWithoutProductionDaemon() {
        let resources = app.descendants(matching: .any)["服务器"]
        XCTAssertTrue(resources.waitForExistence(timeout: 5), "Fixture launch must reach the native resource surface")

        let refreshButton = app.buttons["更新资源数据"]
        XCTAssertTrue(refreshButton.exists, "The native refresh control must be exposed to accessibility")

        let fixtureNotice = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS %@", "正在使用桌面测试夹具")
        ).firstMatch
        XCTAssertTrue(fixtureNotice.waitForExistence(timeout: 2), "The app must identify deterministic fixture mode")

        let startupFailure = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS %@", "无法启动 ServerPilot")
        ).firstMatch
        XCTAssertFalse(
            startupFailure.exists,
            "Fixture mode must return before daemon startup; SERVERPILOT_CLI deliberately points to /usr/bin/false"
        )
    }

    func testResourceTableExposesGPUModelForScheduling() {
        let overviewMetricWindow = app.staticTexts["资源指标：近 10 分钟均值"]
        XCTAssertTrue(
            overviewMetricWindow.waitForExistence(timeout: 5),
            "The resource overview must identify its shared 10-minute metric window"
        )

        let serverRow = app.descendants(matching: .any).matching(
            NSPredicate(format: "label == %@", "服务器 ssh -p 2221 gpu@127.0.0.1")
        ).firstMatch
        XCTAssertTrue(serverRow.waitForExistence(timeout: 5), "Fixture server must appear in the resource table")
        XCTAssertTrue(
            String(describing: serverRow.value).contains("GPU 配置 Fixture GPU"),
            "The resource row must expose the GPU model to visual and accessibility clients"
        )
        XCTAssertTrue(
            String(describing: serverRow.value).contains("CPU 负载"),
            "The resource row must describe normalized host load accurately"
        )

        serverRow.click()
        let serverDetail = app.descendants(matching: .any)["服务器详情"]
        XCTAssertTrue(serverDetail.waitForExistence(timeout: 2), "Selecting a server must open a separate detail sheet")
        let gpuMemoryGrid = app.descendants(matching: .any).matching(
            NSPredicate(format: "label == %@", "GPU 显存状态")
        ).firstMatch
        XCTAssertTrue(gpuMemoryGrid.waitForExistence(timeout: 2), "Server detail must group per-GPU memory rings by state")
        XCTAssertFalse(app.staticTexts["当前观测"].exists, "The detail sheet must not repeat a bulky current-observation summary")

        let closeButton = app.buttons["关闭"].firstMatch
        XCTAssertTrue(closeButton.waitForExistence(timeout: 2), "Server detail must expose an obvious close action")
        closeButton.click()
        XCTAssertTrue(serverRow.waitForExistence(timeout: 2), "Closing detail must preserve the resource table")
        XCTAssertFalse(serverDetail.exists, "The separate server detail sheet must be dismissed")
    }

    func testResourceTableExposesProjectAndCurrentTask() {
        app.terminate()
        app.launchEnvironment["SERVERPILOT_DESKTOP_FIXTURE"] = "resource-ownership"
        app.launch()

        let serverRow = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@", "ssh -p 2222 gpu@10.20.0.21")
        ).firstMatch
        XCTAssertTrue(serverRow.waitForExistence(timeout: 5), "Owned GPU server must appear in the resource table")
        let rowValue = String(describing: serverRow.value)
        XCTAssertTrue(rowValue.contains("vision-lab"), "The resource row must expose the active project")
        XCTAssertTrue(rowValue.contains("train-resnet"), "The resource row must expose the current task")
        XCTAssertFalse(rowValue.contains("agent-trainer"), "The resource row must not expose internal Agent identity")

        serverRow.click()
        assertAgentIdentityIsNotExposed()

        let gpuMemoryGrid = app.descendants(matching: .any).matching(
            NSPredicate(format: "label == %@", "GPU 显存状态")
        ).firstMatch
        XCTAssertTrue(gpuMemoryGrid.waitForExistence(timeout: 2), "Server detail must group each GPU memory ring by state")
        XCTAssertTrue(
            String(describing: gpuMemoryGrid.value).contains("GPU 0 繁忙"),
            "The state grid must expose each GPU's state without a verbose card list"
        )
        XCTAssertFalse(app.buttons["GPU 0"].exists, "Server detail memory rings must not recreate per-GPU detail cards")
    }

    func testOccupancyActionsAndServerOperationsRemainReachable() {
        app.terminate()
        app.launchEnvironment["SERVERPILOT_DESKTOP_FIXTURE"] = "keepalive"
        app.launch()

        let serverRow = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@", "ssh -p 2223 gpu@10.20.0.31")
        ).firstMatch
        XCTAssertTrue(serverRow.waitForExistence(timeout: 5), "Keepalive fixture server must appear in the resource table")
        let rowValue = String(describing: serverRow.value)
        XCTAssertTrue(rowValue.contains("占卡"), "Keepalive must use the user-facing occupancy term")
        XCTAssertTrue(rowValue.contains("2/2 可用"), "Idle occupancy GPUs remain publicly available")
        XCTAssertFalse(rowValue.contains("__serverpilot_system__"), "Internal system identity must not appear in the resource table")

        serverRow.click()
        let gpuMemoryGrid = app.descendants(matching: .any).matching(
            NSPredicate(format: "label == %@", "GPU 显存状态")
        ).firstMatch
        XCTAssertTrue(gpuMemoryGrid.waitForExistence(timeout: 2), "Server detail must expose occupancy through the GPU state group")
        XCTAssertTrue(
            String(describing: gpuMemoryGrid.value).contains("占卡 2 张"),
            "Active occupancy must be conveyed by the corresponding GPU state group, not a duplicate summary row"
        )
        let action = app.buttons["endpoint-keepalive-action"]
        XCTAssertTrue(action.exists, "Configured keepalive must expose exactly one endpoint action")
        XCTAssertEqual(action.label, "结束占卡", "Active occupancy must offer the plainly named end action")

        let operations = app.buttons["服务器操作"]
        XCTAssertTrue(operations.exists, "Server settings must remain reachable from the detail sheet")
        operations.click()
        let editServer = app.menuItems["编辑或移除服务器"]
        XCTAssertTrue(editServer.waitForExistence(timeout: 2))
        editServer.click()
        let remove = app.buttons["endpoint-delete-action"]
        XCTAssertTrue(remove.waitForExistence(timeout: 2))
        XCTAssertEqual(remove.label, "从 ServerPilot 移除…")
        XCTAssertTrue(app.staticTexts["危险操作"].exists)
        XCTAssertFalse(app.menuItems["删除服务器"].exists)
        XCTAssertFalse(app.menuItems["暂停接收新任务"].exists)

        remove.click()
        let confirmation = app.dialogs.firstMatch
        XCTAssertTrue(confirmation.waitForExistence(timeout: 2), "Removal must ask for confirmation")
        XCTAssertTrue(confirmation.staticTexts["从 ServerPilot 移除这台服务器？"].exists)
        confirmation.buttons["取消"].click()
        XCTAssertTrue(remove.waitForExistence(timeout: 2), "Cancel must keep the edit sheet open")

        relaunch(fixture: "keepalive-off", section: "server-pool")
        let inactiveServer = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@", "ssh -p 2224 gpu@10.20.0.32")
        ).firstMatch
        XCTAssertTrue(inactiveServer.waitForExistence(timeout: 5))
        inactiveServer.click()
        let startAction = app.buttons["endpoint-keepalive-action"]
        XCTAssertTrue(startAction.waitForExistence(timeout: 2))
        XCTAssertEqual(startAction.label, "开始占卡", "Inactive occupancy must offer the plainly named start action")
    }

    func testConflictDoesNotHideOtherAvailableGPUs() {
        relaunch(fixture: "conflict-with-available", section: "server-pool")

        let serverRow = app.descendants(matching: .any).matching(
            NSPredicate(format: "value CONTAINS %@", "任务归属待核对")
        ).firstMatch
        XCTAssertTrue(serverRow.waitForExistence(timeout: 5), "Task-attribution review must be explicit in the resource table")
        XCTAssertTrue(
            String(describing: serverRow.value).contains("可申请"),
            "A task-attribution review must not make unrelated available GPUs appear unavailable"
        )
        serverRow.click()
        let recovery = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS %@", "仍可申请")
        ).firstMatch
        XCTAssertTrue(recovery.waitForExistence(timeout: 2), "Server detail must explain that the review is per-GPU")
        XCTAssertTrue(
            app.buttons["任务结束后清理记录"].exists,
            "Cleanup must be explicitly limited to tasks that have ended"
        )

        let gpuMemoryGrid = app.descendants(matching: .any).matching(
            NSPredicate(format: "label == %@", "GPU 显存状态")
        ).firstMatch
        XCTAssertTrue(gpuMemoryGrid.waitForExistence(timeout: 2))
        let gridValue = String(describing: gpuMemoryGrid.value)
        XCTAssertTrue(
            gridValue.contains("GPU 0 繁忙"),
            "An observed worker change must be presented as a busy task, not a hardware error"
        )
        XCTAssertTrue(
            gridValue.contains("观测到进程已更新"),
            "The grid must retain the process observation alongside the task assignment"
        )
        XCTAssertFalse(
            gridValue.contains("GPU 0 错误"),
            "Ordinary worker turnover must not be presented as a GPU error"
        )
    }

    func testKeepaliveRecoveryUsesCanonicalAvailabilityAndDesiredState() {
        relaunch(fixture: "keepalive-recovery", section: "server-pool")

        let serverRow = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@", "ssh -p 2235 gpu@10.20.0.33")
        ).firstMatch
        XCTAssertTrue(serverRow.waitForExistence(timeout: 5))
        serverRow.click()

        let gpuMemoryGrid = app.descendants(matching: .any).matching(
            NSPredicate(format: "label == %@", "GPU 显存状态")
        ).firstMatch
        XCTAssertTrue(gpuMemoryGrid.waitForExistence(timeout: 2))
        let gridValue = String(describing: gpuMemoryGrid.value)
        XCTAssertTrue(
            gridValue.contains("GPU 0 错误"),
            "A keepalive identity error must remain in the explicit error state group"
        )
        XCTAssertFalse(
            gridValue.contains("GPU 0 空闲"),
            "A CONFLICT GPU must never be presented as available"
        )
        XCTAssertTrue(
            gridValue.contains("GPU 1 空闲"),
            "A stopped occupancy helper must remain visibly available in the free state group"
        )
    }

    func testResourceTableHeadersToggleSortDirection() {
        let gpuSort = app.buttons["按GPU 利用率排序"]
        XCTAssertTrue(gpuSort.waitForExistence(timeout: 5), "GPU utilization header must be directly sortable")
        for header in ["按显存占用率排序", "按CPU 负载排序", "按内存占用率排序"] {
            XCTAssertTrue(app.buttons[header].exists, "Resource table must expose the full metric header: \(header)")
        }
        XCTAssertEqual(String(describing: gpuSort.value), "未选中")

        gpuSort.click()
        XCTAssertEqual(String(describing: gpuSort.value), "降序")

        gpuSort.click()
        XCTAssertEqual(String(describing: gpuSort.value), "升序")
    }

    func testAcceptedPrimarySectionsExposeAccessibleContentWithoutAgentUI() {
        relaunch(fixture: "resource-ownership", section: "resource-usage")

        let usage = app.descendants(matching: .any)["使用情况"]
        XCTAssertTrue(usage.waitForExistence(timeout: 5), "Usage must expose its accepted accessible section name")
        XCTAssertTrue(
            app.descendants(matching: .any)["按项目或任务查看使用情况"].exists,
            "Usage must expose project/task switching to accessibility clients"
        )
        assertAgentIdentityIsNotExposed()

        relaunch(fixture: "resource-ownership", section: "settings")

        let settings = app.descendants(matching: .any)["设置"]
        XCTAssertTrue(settings.waitForExistence(timeout: 5), "Settings must expose its accepted accessible section name")
        XCTAssertTrue(app.staticTexts["本机服务地址"].exists)
        XCTAssertTrue(app.staticTexts["数据采集间隔"].exists)
        XCTAssertTrue(app.staticTexts["版本"].exists)
        assertAgentIdentityIsNotExposed()
    }

    func testDeterministicEmptyAndConnectionErrorFixturesExposeTextMeaning() {
        relaunch(fixture: "0", section: "server-pool")

        let emptyState = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS %@", "暂无端点")
        ).firstMatch
        XCTAssertTrue(emptyState.waitForExistence(timeout: 5), "An empty fleet must expose a textual empty state")

        relaunch(fixture: "error", section: "server-pool")

        let failedServer = app.descendants(matching: .any).matching(
            NSPredicate(format: "value CONTAINS %@", "连接失败")
        ).firstMatch
        XCTAssertTrue(
            failedServer.waitForExistence(timeout: 5),
            "Connection failure must be conveyed through accessible text, not color alone"
        )
    }

    func testFrozenViewportsKeepAcceptedNavigationReachable() {
        for viewport in ["1024x640", "1280x800", "1440x820"] {
            relaunch(fixture: "resource-ownership", section: "server-pool", viewport: viewport)

            XCTAssertTrue(app.descendants(matching: .any)["服务器"].waitForExistence(timeout: 5))
            XCTAssertTrue(app.buttons["使用情况"].exists)
            XCTAssertTrue(app.buttons["设置"].exists)
            XCTAssertTrue(app.buttons["更新资源数据"].exists)
            XCTAssertTrue(
                app.buttons["manage-server-groups"].exists,
                "Server group management must stay reachable at \(viewport)"
            )
        }
    }

    func testResourceTableCutsGroupsWithoutTurningServersIntoCards() {
        relaunch(fixture: "server-groups", section: "server-pool")
        let groupHeader = app.descendants(matching: .any)["server-group-header"]
        XCTAssertTrue(groupHeader.waitForExistence(timeout: 5), "Grouped fixtures must expose a server-group section header")
        XCTAssertEqual(groupHeader.label, "夹具实验室")
        XCTAssertTrue(
            String(describing: groupHeader.value).contains("1/1 空闲"),
            "The group header must show a compact available/total GPU summary"
        )

        let serverRow = app.descendants(matching: .any).matching(
            NSPredicate(format: "label == %@", "服务器 ssh -p 2221 gpu@127.0.0.1")
        ).firstMatch
        XCTAssertTrue(serverRow.waitForExistence(timeout: 5), "Grouped servers must remain aligned table rows")
        let rowValue = String(describing: serverRow.value)
        XCTAssertFalse(rowValue.contains("/srv/serverpilot-fixtures"), "The 44 pt row must not carry the full workspace path")
        XCTAssertFalse(rowValue.contains("CUDA 12"), "Environment notes must stay out of the 44 pt row")
        XCTAssertFalse(rowValue.contains("共享权重盘"), "Data/weight notes must stay out of the 44 pt row")
    }

    func testUngroupedServersAppearLastWithActionableLabel() {
        relaunch(fixture: "mixed-resources", section: "server-pool")

        let groupHeader = app.descendants(matching: .any)["server-group-header"]
        XCTAssertTrue(groupHeader.waitForExistence(timeout: 5), "The grouped GPU server must sit under a server-group header")
        XCTAssertEqual(groupHeader.label, "训练服务器组")

        let ungroupedHeader = app.descendants(matching: .any)["ungrouped-server-header"]
        XCTAssertTrue(ungroupedHeader.waitForExistence(timeout: 2), "Ungrouped servers must have an actionable section header")
        XCTAssertTrue(ungroupedHeader.staticTexts["未分组的服务器"].exists)
        XCTAssertTrue(ungroupedHeader.buttons["管理服务器组"].exists)
        XCTAssertLessThan(
            groupHeader.frame.maxY,
            ungroupedHeader.frame.minY,
            "Ungrouped servers must remain last in the table"
        )
    }

    func testServerGroupManagementSheetListsGroupsAndCreateAction() {
        relaunch(fixture: "server-groups", section: "server-pool")
        let manage = app.buttons["manage-server-groups"]
        XCTAssertTrue(manage.waitForExistence(timeout: 5), "Server group management must be reachable from the Servers page")
        manage.click()

        XCTAssertTrue(app.staticTexts["服务器组"].waitForExistence(timeout: 2))
        XCTAssertTrue(app.staticTexts["夹具实验室"].exists)
        XCTAssertTrue(app.staticTexts["/srv/serverpilot-fixtures"].exists)
        XCTAssertTrue(app.staticTexts["CUDA 12，共享权重盘已同步"].exists)
        XCTAssertTrue(app.buttons["添加服务器组"].exists)
        XCTAssertTrue(app.buttons["编辑服务器组 夹具实验室"].exists)

        app.buttons["编辑服务器组 夹具实验室"].click()
        XCTAssertTrue(app.staticTexts["显示名称"].waitForExistence(timeout: 2))
        XCTAssertTrue(app.staticTexts["默认工作区路径"].exists)
        XCTAssertTrue(app.staticTexts["环境说明"].exists)
        XCTAssertTrue(app.staticTexts["说明"].exists)
        XCTAssertTrue(
            app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "不会进入采集")).firstMatch.exists
        )
    }

    func testServerDetailExposesGroupAndFullMetadata() {
        relaunch(fixture: "server-groups", section: "server-pool")
        let serverRow = app.descendants(matching: .any).matching(
            NSPredicate(format: "label == %@", "服务器 ssh -p 2221 gpu@127.0.0.1")
        ).firstMatch
        XCTAssertTrue(serverRow.waitForExistence(timeout: 5))
        serverRow.click()

        XCTAssertTrue(app.staticTexts["服务器组"].waitForExistence(timeout: 2))
        XCTAssertTrue(app.staticTexts["夹具实验室"].exists)
        let groupMetadata = app.descendants(matching: .any)["server-group-metadata"]
        XCTAssertTrue(groupMetadata.waitForExistence(timeout: 2), "Server detail must expose full group metadata")
        XCTAssertTrue(app.staticTexts["组默认工作区"].exists)
        XCTAssertTrue(app.staticTexts["环境说明"].exists)
        XCTAssertTrue(app.staticTexts["CUDA 12，共享权重盘已同步"].exists)
        XCTAssertTrue(app.staticTexts["桌面测试夹具的 GPU 服务器组"].exists)
        XCTAssertTrue(app.staticTexts["继承组默认"].exists)

        let claim = app.buttons["server-detail-claim"]
        XCTAssertTrue(claim.exists)
        XCTAssertTrue(
            String(describing: claim.value).contains("服务器组") || claim.label == "申请 GPU",
            "Grouped claim must remain labeled as applying for GPU"
        )
    }

    func testClaimActionPrefersServerGroupSelection() {
        relaunch(fixture: "server-groups", section: "server-pool")
        let claim = app.buttons["申请 GPU"]
        XCTAssertTrue(claim.waitForExistence(timeout: 5))
        XCTAssertTrue(
            String(describing: claim.value).contains("服务器组"),
            "Grouped direct claims must tell the operator to select a server group first"
        )
        XCTAssertFalse(
            String(describing: claim.value).contains("指定"),
            "The primary claim action must not encourage pinning a specific server"
        )
    }

    private func relaunch(fixture: String, section: String, viewport: String? = nil) {
        app.terminate()
        app.launchEnvironment["SERVERPILOT_DESKTOP_FIXTURE"] = fixture
        app.launchEnvironment["SERVERPILOT_DESKTOP_SECTION"] = section
        if let viewport {
            app.launchEnvironment["SERVERPILOT_DESKTOP_VIEWPORT"] = viewport
        } else {
            app.launchEnvironment.removeValue(forKey: "SERVERPILOT_DESKTOP_VIEWPORT")
        }
        app.launch()
    }

    private func assertAgentIdentityIsNotExposed() {
        let disclosedIdentity = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@ OR value CONTAINS %@", "agent-trainer", "agent-trainer")
        ).firstMatch
        XCTAssertFalse(disclosedIdentity.exists, "Visible, help, and accessibility content must not expose internal Agent identity")
    }
}
