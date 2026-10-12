#!/usr/bin/env bash
# Read-only compatibility inventory. Run on the Switch, not on the build PC.
# No sudo, downloads, installation, device writes, or configuration changes.
# The optional graphics queries initialize the installed graphics driver.

if [[ -z ${BASH_VERSION:-} ]] || (( BASH_VERSINFO[0] < 4 )); then
    printf 'ERROR: Run this script with Bash 4 or newer.\n' >&2
    exit 2
fi

# Continue after a missing tool or unavailable kernel interface.
set -uo pipefail
export LC_ALL=C
graphics=0
warnings=0

usage() {
    local help_line
    while IFS= read -r help_line; do printf '%s\n' "$help_line"; done <<'EOF'
Usage: bash probe-switch.sh [--graphics] [--help]

Run from an ordinary user account inside Switchroot Linux on the Switch.
The report goes to stdout; save it with shell redirection if desired:
  bash probe-switch.sh > switch-report.txt
  bash probe-switch.sh --graphics > switch-report-graphics.txt

Default: inspect kernel, device tree, OS, CPU, memory, NVIDIA module metadata,
GPU device nodes, relevant installed packages, and block-device layout.
--graphics: also query installed vulkaninfo and glxinfo -B, with
            a 20-second timeout per query. Run in your graphical session.
            Shader disk caches are disabled for these queries. No tools
            are installed. Steam, gamescope, and games are never launched.
--help:     show this help.

No sudo is needed. No devices or configuration are written. Hostname, user
name, network settings, disk UUID/serial numbers and mount paths are omitted.
Graphics command output is restricted to driver/capability fields. Missing
interfaces/tools are reported and the remaining diagnostics continue.

Exit status: 0 = report completed (may include warnings), 2 = invalid usage
or unsupported Bash. A report does not certify SteamOS/game compatibility.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --graphics) graphics=1 ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'ERROR: Unknown option. Use --help.\n' >&2; exit 2 ;;
    esac
done

section() { printf '\n=== %s ===\n' "$1"; }
warn() { printf 'WARNING: %s\n' "$*"; warnings=$((warnings + 1)); }
have() { command -v "$1" >/dev/null 2>&1; }

# Do not echo raw stderr: graphics tools can include personal filesystem paths.
query() {
    local label=$1
    local rc
    shift
    printf '%s:\n' "$label"
    if ! have "$1"; then
        warn "$1 is not installed or not in PATH."
        return 0
    fi
    "$@" 2>/dev/null
    rc=$?
    if (( rc != 0 )); then
        warn "$label returned status $rc."
    fi
}

printf 'Switchroot / SteamOS ARM64 compatibility report v1\n'
printf 'Read-only inventory; no Steam or gamescope launch.\n'

section 'Kernel and architecture'
query 'Kernel (system, release, architecture)' uname -s -r -m
arch=$(uname -m 2>/dev/null || true)
case "$arch" in
    aarch64|arm64) printf 'ARM64 kernel: yes\n' ;;
    *) warn 'This is not an ARM64 kernel; collect the real report on Switchroot.' ;;
esac
section 'Device tree (model and compatible only)'
dt_root=''
for candidate in /sys/firmware/devicetree/base /proc/device-tree; do
    if [[ -d $candidate ]]; then dt_root=$candidate; break; fi
done
if [[ -z $dt_root ]]; then
    warn 'Device tree is unavailable.'
elif ! have tr; then
    warn 'tr is unavailable; cannot decode device-tree strings.'
else
    for property in model compatible; do
        printf '%s:\n' "$property"
        if [[ -r $dt_root/$property ]]; then
            tr '\000' '\n' < "$dt_root/$property"
            printf '\n'
        else
            warn "Device-tree $property is not readable."
        fi
    done
fi
printf 'Mariko board revision must be confirmed from the model/compatible above.\n'

section 'Operating system'
if [[ -r /etc/os-release ]] && have awk; then
    # Read as text, never source a system file into the diagnostic shell.
    awk '/^(NAME|PRETTY_NAME|ID|ID_LIKE|VERSION|VERSION_ID|VERSION_CODENAME|BUILD_ID)=/' /etc/os-release
else
    warn '/etc/os-release or awk is unavailable.'
fi
query 'Userspace word size' getconf LONG_BIT
query 'Page size in bytes' getconf PAGESIZE

section 'CPU capabilities'
if [[ -r /proc/cpuinfo ]] && have awk; then
    awk '
        /^processor[[:space:]]*:/ { cpus++ }
        /^(Features|flags|CPU implementer|CPU architecture|CPU variant|CPU part|CPU revision)[[:space:]]*:/ {
            if (!seen[$0]++) print
        }
        END { if (cpus) printf "Logical CPUs listed: %d\n", cpus }
    ' /proc/cpuinfo
else
    warn '/proc/cpuinfo or awk is unavailable.'
fi
printf 'ARM64 alone does not establish Steam client or x86 translation compatibility.\n'

section 'Memory'
if [[ -r /proc/meminfo ]] && have awk; then
    awk '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/' /proc/meminfo
else
    warn '/proc/meminfo or awk is unavailable.'
fi

section 'NVIDIA / Tegra release and kernel module'
if [[ -r /etc/nv_tegra_release ]]; then
    query 'nv_tegra_release' cat /etc/nv_tegra_release
else
    warn '/etc/nv_tegra_release is unavailable.'
fi
if [[ -d /sys/module/nvgpu ]]; then
    printf 'nvgpu present in the running kernel: yes\n'
else
    printf 'nvgpu present in the running kernel: no\n'
fi
if have modinfo; then
    for field in version vermagic depends; do
        query "nvgpu $field (an empty version is possible)" modinfo -F "$field" nvgpu
    done
else
    warn 'modinfo is unavailable; nvgpu module metadata was not queried.'
fi

section 'GPU device nodes (no owner names)'
shopt -s nullglob
gpu_nodes=(/dev/dri/card* /dev/dri/renderD* /dev/nvhost-* /dev/nvmap /dev/nvgpu /dev/nvidia*)
node_count=0
for node in "${gpu_nodes[@]}"; do
    [[ -e $node ]] || continue
    readable=no
    writable=no
    [[ -r $node ]] && readable=yes
    [[ -w $node ]] && writable=yes
    printf '%s readable=%s writable=%s\n' "$node" "$readable" "$writable"
    node_count=$((node_count + 1))
done
if (( node_count == 0 )); then warn 'No matching GPU device nodes found.'; fi

section 'Relevant installed packages'
if have dpkg-query && have awk; then
    dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Status}\n' 2>/dev/null |
        awk '$4 == "installed" && $1 ~ /(nvidia|tegra|l4t|switchroot|gamescope|mesa|vulkan|box64|fex|steam)/ { print; count++ }
             END { if (!count) print "No matching installed packages reported." }'
    query_status=${PIPESTATUS[0]}
    if (( query_status != 0 )); then warn "dpkg-query returned status $query_status."; fi
else
    warn 'dpkg-query or awk is unavailable; package inventory skipped.'
fi

section 'Block devices (no UUID, serial, labels, or mount paths)'
query 'Device layout' lsblk --output NAME,TYPE,SIZE,FSTYPE,RO

section 'Optional graphics capabilities'
if (( graphics == 0 )); then
    printf 'Skipped. Add --graphics inside the graphical session to query Vulkan/OpenGL.\n'
elif ! have timeout || ! have awk || ! have env; then
    warn 'timeout, awk and env are required for bounded graphics queries; skipped.'
else
    if have vulkaninfo; then
        printf 'vulkaninfo (selected capability fields and gamescope requirements):\n'
        timeout --kill-after=2s 20s env __GL_SHADER_DISK_CACHE=0 MESA_SHADER_CACHE_DISABLE=true vulkaninfo 2>/dev/null |
            awk '/^[[:space:]]*(Vulkan Instance Version:|GPU[0-9]+:|apiVersion[[:space:]]*=|driverVersion[[:space:]]*=|vendorID[[:space:]]*=|deviceID[[:space:]]*=|deviceType[[:space:]]*=|deviceName[[:space:]]*=|driverID[[:space:]]*=|driverName[[:space:]]*=|driverInfo[[:space:]]*=|conformanceVersion[[:space:]]*=|VK_KHR_external_memory_fd[[:space:]]|VK_EXT_external_memory_dma_buf[[:space:]]|VK_KHR_external_semaphore_fd[[:space:]]|VK_EXT_robustness2[[:space:]]|VK_KHR_dynamic_rendering[[:space:]]|VK_KHR_present_id[[:space:]]|VK_KHR_present_wait[[:space:]]|VK_EXT_image_drm_format_modifier[[:space:]]|VK_EXT_physical_device_drm[[:space:]]|VK_EXT_queue_family_foreign[[:space:]]|dynamicRendering[[:space:]]*=|timelineSemaphore[[:space:]]*=|scalarBlockLayout[[:space:]]*=|shaderFloat16[[:space:]]*=|shaderInt16[[:space:]]*=)/ { print; count++ }
                 END { if (!count) print "No recognized Vulkan capability fields returned." }'
        query_status=${PIPESTATUS[0]}
        if (( query_status == 124 || query_status == 137 )); then
            warn 'Vulkan query exceeded its time limit.'
        elif (( query_status != 0 )); then
            warn "Vulkan query returned status $query_status (driver/session/tool issue)."
        fi
    else
        warn 'vulkaninfo is unavailable; Vulkan query skipped.'
    fi
    if have glxinfo; then
        printf 'glxinfo -B (selected capability fields):\n'
        timeout --kill-after=2s 20s env __GL_SHADER_DISK_CACHE=0 MESA_SHADER_CACHE_DISABLE=true glxinfo -B 2>/dev/null |
            awk '/^[[:space:]]*(direct rendering:|Vendor:|Device:|Version:|Accelerated:|Video memory:|Unified memory:|Preferred profile:|Max core profile version:|Max compat profile version:|Max GLES[0-9]* profile version:|OpenGL vendor string:|OpenGL renderer string:|OpenGL version string:|OpenGL shading language version string:|OpenGL core profile version string:|OpenGL core profile shading language version string:|OpenGL ES profile version string:)/ { print; count++ }
                 END { if (!count) print "No recognized OpenGL capability fields returned." }'
        query_status=${PIPESTATUS[0]}
        if (( query_status == 124 || query_status == 137 )); then
            warn 'OpenGL query exceeded its time limit.'
        elif (( query_status != 0 )); then
            warn "OpenGL query returned status $query_status (an X display may be unavailable)."
        fi
    else
        warn 'glxinfo is unavailable; OpenGL query skipped.'
    fi
fi

section 'Result'
printf 'Inventory complete; warnings: %d.\n' "$warnings"
printf 'Boot, input, audio, suspend, gamescope, Steam and games are not tested.\n'
printf 'A successful graphics query is not proof that a SteamOS port will boot or run games.\n'
exit 0
