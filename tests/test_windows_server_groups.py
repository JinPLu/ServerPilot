"""Windows desktop locks the accepted server-group presentation."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_JS = (ROOT / "desktop" / "windows" / "ui" / "app.js").read_text(encoding="utf-8")
WINDOWS_HTML = (ROOT / "desktop" / "windows" / "ui" / "index.html").read_text(encoding="utf-8")
LAUNCHER = (ROOT / "desktop" / "windows_launcher.py").read_text(encoding="utf-8")


def test_windows_table_groups_servers_with_ungrouped_last() -> None:
    assert "function groupedSections" in WINDOWS_JS
    assert 'sections.push({ group: null, ungrouped: true, items: ungrouped })' in WINDOWS_JS
    assert "storage_group" not in WINDOWS_JS
    assert "class=\"group-header" in WINDOWS_JS
    assert "未分组" in WINDOWS_JS
    assert "id=\"open-groups\"" in WINDOWS_HTML
    assert "id=\"claim-group\"" in WINDOWS_HTML
    assert "按单机申请" in WINDOWS_HTML


def test_windows_claim_ui_prefers_server_group_over_exact_grouped_server() -> None:
    assert "constraints.server_group_ids = [groupID]" in WINDOWS_JS
    assert "same_host: true" in WINDOWS_JS
    assert 'groupID === "__ungrouped__"' in WINDOWS_JS
    assert "if (!endpoint)" in WINDOWS_JS
    assert "constraints.endpoint_ids = [endpoint]" in WINDOWS_JS
    assert "未分组申请必须选择一台未分组服务器" in WINDOWS_JS
    assert "外部调度器" not in WINDOWS_JS
    assert "外部调度器" not in WINDOWS_HTML
    assert "scheduler_targets" not in WINDOWS_JS
    assert "resource_claims" not in WINDOWS_JS
    assert "best-fit" in WINDOWS_HTML or "best-fit" in WINDOWS_JS
    claim_allowlist = LAUNCHER.split("_CLAIM_CONSTRAINT_FIELDS = {", 1)[1].split("}", 1)[0]
    assert '"same_host"' in claim_allowlist


def test_windows_add_and_edit_bind_group_and_workspace_override() -> None:
    assert "workspace_path_override" in WINDOWS_JS
    assert "server_group_id" in WINDOWS_JS
    assert "payload.workspace_path_override = trimmed || null" in WINDOWS_JS
    assert "payload.workspace_path = trimmed" in WINDOWS_JS
    write_payload = WINDOWS_JS.split("function endpointWritePayload", 1)[1].split("function memoryColor", 1)[0]
    assert "payload.workspace_path_override = trimmed || null" in write_payload
    grouped_branch = write_payload.split("if (groupID)", 1)[1].split("} else {", 1)[0]
    assert "workspace_path_override" in grouped_branch
    assert "payload.workspace_path =" not in grouped_branch
    ungrouped_branch = write_payload.split("} else {", 1)[1]
    assert "payload.workspace_path = trimmed" in ungrouped_branch
    assert "workspace_path_override" not in ungrouped_branch
    assert "id=\"add-server-group\"" in WINDOWS_HTML
    assert "id=\"edit-server-group\"" in WINDOWS_HTML
    assert "继承组默认" in WINDOWS_HTML
    assert "environmentNotes" in WINDOWS_HTML
    group_id = next(line for line in WINDOWS_HTML.splitlines() if 'id="group-id"' in line)
    assert 'pattern="[a-z][a-z0-9-]{1,127}"' in group_id
    group_description = next(line for line in WINDOWS_HTML.splitlines() if 'id="group-description"' in line)
    assert 'maxlength="1000"' in group_description
    group_notes = next(line for line in WINDOWS_HTML.splitlines() if 'id="group-environment-notes"' in line)
    assert 'maxlength="8000"' in group_notes
    assert "environment_notes" in WINDOWS_JS
    assert "不会写入进程环境" in WINDOWS_HTML or "不会传入采集" in WINDOWS_HTML
    assert 'value="/srv/serverpilot-workspace"' not in WINDOWS_HTML
    add_workspace = next(line for line in WINDOWS_HTML.splitlines() if 'id="add-workspace"' in line)
    assert "value=" not in add_workspace
    assert " required" not in add_workspace
    assert "未分组服务器必须填写绝对远端工作区路径" in WINDOWS_JS
    assert "/srv/serverpilot-workspace" not in WINDOWS_JS
    assert "workspaceInput.value =" not in WINDOWS_JS


def test_windows_launcher_allowlists_canonical_group_fields() -> None:
    assert '"server_group_id"' in LAUNCHER
    assert '"server_group_ids"' in LAUNCHER
    assert '"workspace_path_override"' in LAUNCHER
    assert "/api/v1/server-groups" in LAUNCHER
    assert "environment_notes is JSON metadata" in LAUNCHER
    assert "never copied into os.environ" in LAUNCHER
    create_endpoint_block = LAUNCHER.split("def create_endpoint", 1)[1].split("def update_endpoint", 1)[0]
    assert "environment_notes" not in create_endpoint_block
    assert "_endpoint_mutation_payload" in LAUNCHER
    sanitizer = LAUNCHER.split("def _endpoint_mutation_payload", 1)[1].split("def _invalid", 1)[0]
    assert 'body.pop("workspace_path", None)' in sanitizer
    assert 'body.pop("workspace_path_override", None)' in sanitizer
    claim_allowlist = LAUNCHER.split("_CLAIM_CONSTRAINT_FIELDS = {", 1)[1].split("}", 1)[0]
    assert '"server_group_ids"' in claim_allowlist
    assert '"same_host"' in claim_allowlist
    assert '"server_group_id"' not in claim_allowlist
