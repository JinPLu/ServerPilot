"""The single sealed Linux/NVIDIA probe used by direct and delegated transports."""

GPU_QUERY = (
    "nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,memory.free,"
    "utilization.gpu,utilization.memory,temperature.gpu,power.draw,pstate,pci.bus_id "
    "--format=csv,noheader,nounits"
)
PROCESS_QUERY = (
    "nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory,process_name "
    "--format=csv,noheader,nounits"
)
IDENTITY_QUERY = "hostname; cat /proc/sys/kernel/random/boot_id"
HOST_RESOURCES_QUERY = (
    "getconf _NPROCESSORS_ONLN; "
    "awk '/MemTotal:/{total=$2} /MemAvailable:/{available=$2} "
    "END {printf \"%d %d\\n\", total/1024, available/1024}' /proc/meminfo; "
    "cut -d ' ' -f1 /proc/loadavg; "
    "awk 'NR == 1 && $1 == \"cpu\" {idle=$5+$6; total=0; "
    "for (i=2; i<=NF; i++) total+=$i; printf \"%.0f %.0f\\n\", total, idle}' /proc/stat; "
    "if [ -r /sys/fs/cgroup/cpu.max ] && [ -r /sys/fs/cgroup/cpu.stat ]; then "
    "awk 'FNR==NR {quota=$1; period=$2; next} "
    "$1==\"usage_usec\" && quota != \"\" && period != \"\" "
    "{printf \"%s %s %s\\n\", quota, period, $2}' "
    "/sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat; fi; "
    "if [ -r /sys/fs/cgroup/memory.max ] && [ -r /sys/fs/cgroup/memory.current ]; then "
    "printf \"mem %s %s\\n\" "
    "\"$(cat /sys/fs/cgroup/memory.max)\" "
    "\"$(cat /sys/fs/cgroup/memory.current)\"; fi"
)
GPU_SECTION = "__SERVERPILOT_GPU__"
PROCESS_SECTION = "__SERVERPILOT_PROCESSES__"
PROCESS_DETAILS_SECTION = "__SERVERPILOT_PROCESS_DETAILS__"
IDENTITY_SECTION = "__SERVERPILOT_IDENTITY__"
HOST_RESOURCES_SECTION = "__SERVERPILOT_HOST_RESOURCES__"
GPU_UNAVAILABLE = "__SERVERPILOT_GPU_UNAVAILABLE__"
GPU_CPU_ONLY = "__SERVERPILOT_GPU_CPU_ONLY__"
RAW_SSH_COMBINED_QUERY = (
    f"set -e; printf '{GPU_SECTION}\\n'; "
    f"if command -v nvidia-smi >/dev/null 2>&1; then "
    f"if serverpilot_gpu_output=$({GPU_QUERY} 2>/dev/null); then serverpilot_gpu_rc=0; "
    "else serverpilot_gpu_rc=$?; fi; "
    "if [ \"$serverpilot_gpu_rc\" = 0 ] && [ -n \"$serverpilot_gpu_output\" ]; then "
    "serverpilot_nvidia=1; printf '%s\\n' \"$serverpilot_gpu_output\"; "
    "elif [ \"$serverpilot_gpu_rc\" = 0 ]; then "
    f"serverpilot_nvidia=0; printf '{GPU_CPU_ONLY}\\n'; "
    f"else serverpilot_nvidia=0; printf '{GPU_UNAVAILABLE}\\n'; fi; "
    f"else serverpilot_nvidia=0; printf '{GPU_CPU_ONLY}\\n'; fi; "
    f"printf '{PROCESS_SECTION}\\n'; "
    "if [ \"$serverpilot_nvidia\" = 1 ]; then "
    f"serverpilot_processes=$({PROCESS_QUERY}); "
    "printf '%s\\n' \"$serverpilot_processes\"; "
    "else serverpilot_processes=''; fi; "
    f"printf '{PROCESS_DETAILS_SECTION}\\n'; "
    "serverpilot_pids=$(printf '%s\\n' \"$serverpilot_processes\" | "
    "awk -F',' '{pid=$2; gsub(/^[ \\t]+|[ \\t]+$/, \"\", pid); "
    "if (pid ~ /^[0-9]+$/ && pid > 0 && !seen[pid]++) "
    "{printf \"%s%s\", separator, pid; separator=\",\"}}'); "
    "if [ -n \"$serverpilot_pids\" ]; then "
    "serverpilot_ps_output=$(ps -o pid=,user=,etimes=,comm= "
    "-p \"$serverpilot_pids\" 2>&1) && serverpilot_ps_rc=0 || serverpilot_ps_rc=$?; "
    "if [ \"$serverpilot_ps_rc\" = 0 ]; then printf '%s\\n' \"$serverpilot_ps_output\"; "
    "elif [ \"$serverpilot_ps_rc\" != 1 ] || [ -n \"$serverpilot_ps_output\" ]; then "
    "printf '%s\\n' \"$serverpilot_ps_output\" >&2; exit \"$serverpilot_ps_rc\"; fi; fi; "
    f"printf '{IDENTITY_SECTION}\\n'; {IDENTITY_QUERY}; "
    f"printf '{HOST_RESOURCES_SECTION}\\n'; {HOST_RESOURCES_QUERY}"
)



if __name__ == "__main__":
    import re
    from pathlib import Path
    plugin = Path(__file__).with_name("bundled_plugins") / "slurm-immediate"
    source = plugin.read_text()
    source = re.sub(r"^NODE_PROBE = .*?$", lambda _: "NODE_PROBE = " + repr(RAW_SSH_COMBINED_QUERY),
                    source, count=1, flags=re.MULTILINE)
    plugin.write_text(source)
