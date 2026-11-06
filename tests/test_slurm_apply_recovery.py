"""Exercise the real remote helper for an immediate Slurm allocation failure."""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import time
import types
from pathlib import Path

import pytest

from serverpilot.plugins import bundled_plugin_dir


def remote_helper(tmp_path: Path):
    path = bundled_plugin_dir() / "slurm-immediate"
    plugin = types.ModuleType("slurm_plugin")
    exec(compile(path.read_text(), str(path), "exec"), plugin.__dict__)
    helper = types.ModuleType("slurm_remote")
    helper.__dict__.update(json=json, os=os, re=re, sys=sys, time=time, Path=Path, ROOT=tmp_path,
                           PARTITION="gpu", QOS="", ACCOUNT="", CPUS_PER_GPU=8, MEM_GIB_PER_GPU=16,
                           APPLY_IMMEDIATE_SECONDS=25, NO_CAPACITY_EXIT_CODE=3, HOLD_SCRIPT="pass")
    functions = ast.Module(body=[n for n in ast.parse(plugin.REMOTE_HELPER).body
                                 if isinstance(n, ast.FunctionDef)], type_ignores=[])
    exec(compile(functions, "slurm-remote", "exec"), helper.__dict__)
    return helper


def test_observe_recovers_job_that_never_started_hold(tmp_path):
    helper = remote_helper(tmp_path)
    request_id = "b348961a5181b93e5093c9643804168e"
    request = {"request_id": request_id, "allocation_ref": None}
    (tmp_path / f"request-{request_id}.json").write_text(json.dumps(request))
    (tmp_path / f"request-{request_id}.log").write_text(
        "srun: job 1318807 queued and waiting for resources\n"
        "srun: error: Unable to allocate resources: Requested nodes are busy\n")
    helper.job_details = lambda job: {"JobName": "sp-" + request_id, "JobState": "CANCELLED"}
    helper.subprocess = types.SimpleNamespace(run=lambda *a, **kw: types.SimpleNamespace(returncode=0, stdout=""))
    helper.allocation_fact = lambda job, request, sample: {
        "allocation_ref": job, "request_id": request["request_id"], "state": "released"}
    facts = helper.all_allocations()
    assert facts == [{"allocation_ref": "1318807", "request_id": request_id,
                      "state": "released", "accounting": None}]
    # observe is read-only, and a coincident job number with another name is rejected.
    assert json.loads((tmp_path / f"request-{request_id}.json").read_text()) == request
    helper.job_details = lambda job: {"JobName": "someone-else"}
    assert helper.request_job_id(request) is None


@pytest.mark.parametrize("exit_code", [1, 3])
def test_apply_uses_slurm_dedicated_immediate_exit_code(tmp_path, monkeypatch, exit_code):
    helper = remote_helper(tmp_path)
    helper.cluster_gpu_capacity = lambda: {"largest_free_block": 4}
    monkeypatch.setattr(sys, "argv", ["-", "apply", "1", "3600", "request-id"])
    calls = []
    def popen(argv, **kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(poll=lambda: exit_code)
    helper.subprocess = types.SimpleNamespace(Popen=popen, DEVNULL=-3)
    if exit_code == 3:
        with pytest.raises(SystemExit) as error:
            helper.cmd_apply()
        assert error.value.code == 3
    else:
        with pytest.raises(RuntimeError, match="original request retained"):
            helper.cmd_apply()
    assert calls[0]["env"]["SLURM_EXIT_IMMEDIATE"] == "3"
    assert calls[0]["env"]["SLURM_EXIT_ERROR"] == "1"
