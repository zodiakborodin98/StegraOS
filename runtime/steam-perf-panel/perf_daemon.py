#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Stegra Control root backend (M10T2).

Live CPU/GPU domain limits, read-only EMC telemetry and preparation of explicit
boot entries for CPU/GPU/RAM overclock ceilings.  No client-supplied path,
command, voltage or boot file is accepted.
"""
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import threading
import time

import catalog
import zram_control
from common import atomic_write_json, log_event, read_json, normalise_settings, SETTINGS_PATH

CPU = Path('/sys/devices/system/cpu/cpufreq/policy0')
GPU = Path('/sys/class/devfreq/57000000.gpu')
GPU_LOAD = Path('/sys/devices/57000000.gpu/load')
EMC_RATE = Path('/sys/kernel/debug/tegra_bwmgr/emc_rate')
EMC_MIN = Path('/sys/kernel/debug/tegra_bwmgr/emc_min_rate')
EMC_MAX = Path('/sys/kernel/debug/tegra_bwmgr/emc_max_rate')
EMC_CLK = Path('/sys/kernel/debug/clk/emc/clk_rate')
DVFS_TABLE = Path('/sys/kernel/debug/tegra_dvfs/dvfs_table')
REGULATOR_SUMMARY = Path('/sys/kernel/debug/regulator/regulator_summary')
THERMAL = Path('/sys/class/thermal')
STATE_DIR = Path('/var/lib/steam-perf-panel')
BASELINE = STATE_DIR / 'm10t2-baseline.json'
STATE = STATE_DIR / 'm10t2-state.json'
LOCK = STATE_DIR / 'daemon.lock'
RUNTIME = Path('/run/steam-perf-panel')
SOCKET = RUNTIME / 'control.sock'
BOOT = Path('/run/initramfs/mounts/boot')
INI_DIR = BOOT / 'bootloader/ini'
MAIN_INI = INI_DIR / 'SWITCHDECK-ALPHA.ini'
BOOT_BACKUP = STATE_DIR / 'boot-entries'
CATALOG_PATH = Path(os.environ.get(
    'M10T2_CATALOG', '/usr/local/lib/steam-perf-panel/clock-tables-mhz.yaml'))

CPU_FILES = {'governor': CPU / 'scaling_governor',
             'min': CPU / 'scaling_min_freq',
             'max': CPU / 'scaling_max_freq'}
GPU_FILES = {'governor': GPU / 'governor',
             'min': GPU / 'min_freq',
             'max': GPU / 'max_freq'}
ALLOWED = {p.resolve() for p in [*CPU_FILES.values(), *GPU_FILES.values()]}

# Switch Lite (Vali) boot-script hard limits, from our own boot.generated.txt:
# dvfsb=1 -> CPU 2091 MHz / GPU 844 MHz; 2193+ / 921+ are not offered on Lite.
LITE_CPU_MAX_KHZ = 2091000
LITE_GPU_MAX_HZ = 844800000
STOCK_CPU_MAX_KHZ = 1963500
# CPU 2091 was exposed by the OC boot profile but clamped by the DFLL/DVFS
# boundary. It is deliberately no longer an offered profile.
CPU_PROFILE_MAX_KHZ = STOCK_CPU_MAX_KHZ
STOCK_GPU_MAX_HZ = 768000000
STOCK_EMC_MAX_HZ = 1866000000
PREPARABLE_RAM_KHZ = {1600000, 1866000, 2133000}
OC_INI_NAME = 'STEGRA-OC.ini'

STOP = False
LOCK_OBJ = threading.Lock()
CATALOG = None
BASELINE_CACHE = None
STARTUP_RESTORE = {}


def say(event, **fields):
    log_event('STEGRA_CONTROL', event, **fields)


def text(path):
    return Path(path).read_text().strip()


def number(path):
    return int(text(path))


def values(path):
    return [int(item) for item in text(path).split()]


def get_catalog():
    global CATALOG
    if CATALOG is None:
        CATALOG = catalog.load_catalog(CATALOG_PATH)
    return CATALOG


def wait_for_clocks(timeout=30.0):
    """Wait for late GPU sysfs registration before resolving write allowlist."""
    global ALLOWED
    deadline=time.monotonic()+timeout
    while True:
        if STOP:
            raise SystemExit(0)
        try:
            caps=capture()
            ALLOWED={p.resolve(strict=True) for p in [*CPU_FILES.values(),*GPU_FILES.values()]}
            return caps
        except (OSError,ValueError) as exc:
            if time.monotonic()>=deadline:
                raise RuntimeError('Clock interfaces not ready before deadline: %s' % exc) from exc
            time.sleep(0.25)


def saved_choices(path=SETTINGS_PATH):
    """Read bounded regular JSON; never follow a final symlink as root."""
    try:
        fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd,'r',encoding='utf-8') as handle:
        meta=os.fstat(handle.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid not in (0,1000) or meta.st_size>65536:
            raise ValueError('Invalid saved profile file')
        data=json.load(handle)
        if not isinstance(data,dict):
            raise ValueError('Saved profile must be an object')
        for key in ('cpu_selected','gpu_selected','ram_selected'):
            if key in data and data[key] is not None and type(data[key]) is not int:
                raise ValueError('Invalid saved frequency: '+key)
        settings=normalise_settings(data)
        return {domain:settings[domain+'_selected'] for domain in ('cpu','gpu','ram')}


def restore_saved_policy():
    """Restore only supported CPU/GPU limits; RAM and boot entries are read-only."""
    try:
        choices=saved_choices()
    except (OSError,ValueError) as exc:
        say('BOOT_PROFILE_READ_ERROR',error=str(exc))
        return {'settings':{'ok':False,'status':'error','reason':str(exc)}}
    if choices is None:
        return {}
    report={}
    for domain in ('cpu','gpu'):
        value=choices[domain]
        if value is not None and type(value) is not int:
            result={'ok':False,'status':'error','reason':'Invalid saved frequency'}
        else:
            try:
                result=apply_domain(domain,value)
            except (OSError,ValueError,RuntimeError) as exc:
                result={'ok':False,'status':'error','reason':str(exc)}
        report[domain]=dict(result,requested=value)
    value=choices['ram']
    current=(capture()['emc_max'] or 0)//1000
    expected=(read_baseline()['emc_max']//1000) if value is None else value
    if expected==current:
        report['ram']={'ok':True,'status':'active','requested':value,'actual':current}
    else:
        report['ram']={'ok':False,'status':'restart_required','requested':value,
                       'actual':current,'reason':'Select the matching RAM entry in Hekate; RAM is not changed at runtime.'}
    say('BOOT_PROFILE_RESULT',domains=report)
    return report


def capture():
    cpu_avail = values(CPU / 'scaling_available_frequencies')
    gpu_avail = values(GPU / 'available_frequencies')
    state = {
        'cpu_governor': text(CPU_FILES['governor']),
        'cpu_min': number(CPU_FILES['min']),
        'cpu_max': number(CPU_FILES['max']),
        'cpu_cur': number(CPU / 'cpuinfo_cur_freq'),
        'cpu_scaling_cur': number(CPU / 'scaling_cur_freq'),
        'cpuinfo_max': number(CPU / 'cpuinfo_max_freq'),
        'cpu_available': cpu_avail,
        'cpu_governors': text(CPU / 'scaling_available_governors').split(),
        'gpu_governor': text(GPU_FILES['governor']),
        'gpu_min': number(GPU_FILES['min']),
        'gpu_max': number(GPU_FILES['max']),
        'gpu_cur': number(GPU / 'cur_freq'),
        'gpu_target': number(GPU / 'target_freq'),
        'gpu_available': gpu_avail,
        'gpu_governors': text(GPU / 'available_governors').split(),
        'gpu_load': int(text(GPU_LOAD)) if GPU_LOAD.exists() else None,
        'emc_cur': None,
        'emc_min': None,
        'emc_max': None,
    }
    for key, path in (('emc_cur', EMC_RATE), ('emc_min', EMC_MIN), ('emc_max', EMC_MAX)):
        try:
            state[key] = number(path)
        except (OSError, ValueError):
            pass
    if state['emc_cur'] is None:
        try:
            state['emc_cur'] = number(EMC_CLK)
        except (OSError, ValueError):
            pass
    return state


def thermal():
    zones = {}
    for zone in sorted(THERMAL.glob('thermal_zone*')):
        try:
            zones[text(zone / 'type')] = round(number(zone / 'temp') / 1000.0, 1)
        except (OSError, ValueError):
            continue
    cooling = {}
    for item in sorted(THERMAL.glob('cooling_device*')):
        try:
            cooling[text(item / 'type')] = {
                'cur': number(item / 'cur_state'),
                'max': number(item / 'max_state'),
            }
        except (OSError, ValueError):
            continue
    return {'zones_c': zones, 'cooling': cooling}


def cpu_rail_capability(caps=None):
    """Map CPU OPPs to the real VDD_CPU rail limit.

    The kernel exposes nominal 2091 MHz in cpuinfo_max on the OC boot entry,
    but the 2091 MHz OPP needs 1610 mV while VDD_CPU is limited to 1520 mV on
    this board.  The cpufreq driver silently clamps such writes to 1963.5 MHz.
    This probe makes that boundary explicit instead of treating a clamps as a
    failed apply.
    """
    caps = caps or capture()
    try:
        cclk = []
        vdd_cpu = []
        for line in DVFS_TABLE.read_text(errors='replace').splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == 'cclk_g':
                cclk = [int(round(float(value) * 1000)) for value in parts[1:]]
            elif (parts[0] == 'vdd-cpu' and len(parts) > 2 and
                  parts[1] not in ('table', '(dfll)')):
                nums = [int(value) for value in parts[1:] if value.isdigit()]
                if len(nums) >= 10:
                    vdd_cpu = nums
        rail_max = None
        for line in REGULATOR_SUMMARY.read_text(errors='replace').splitlines():
            if line.strip().startswith('vdd-cpu'):
                mvs = [int(value) for value in re.findall(r'(\d+)mV', line)]
                if mvs:
                    rail_max = max(mvs)
        opp = {khz: mv for khz, mv in zip(cclk, vdd_cpu)}
        available = caps.get('cpu_available') or []
        supported = max([khz for khz in available
                         if opp.get(khz) is not None and rail_max is not None
                         and opp[khz] <= rail_max], default=STOCK_CPU_MAX_KHZ)
        opp_2091 = opp.get(2091000)
        clamp_reason = None
        if caps.get('cpuinfo_max', 0) > STOCK_CPU_MAX_KHZ:
            try:
                dmesg = subprocess.run(['/bin/dmesg'], capture_output=True,
                                       text=True, timeout=3).stdout
            except (OSError, subprocess.TimeoutExpired):
                dmesg = ''
            if "couldn't get vdd_cpu regulator" in dmesg:
                supported = min(supported, STOCK_CPU_MAX_KHZ)
                clamp_reason = (
                    'CPU 2091 MHz is clamped to 1963.5 MHz by the current '
                    'kernel/DT: DFLL could not acquire the vdd_cpu regulator. '
                    'This is a boot-stack/kernel capability boundary, not a '
                    'missing voltage setting; no voltage write was performed.')
        return {
            'supported_max_khz': supported,
            'supported_max_mhz': supported / 1000.0,
            'rail_max_mv': rail_max,
            'opp_2091_mv': opp_2091,
            'clamped': clamp_reason is not None,
            'reason': clamp_reason,
        }
    except (OSError, ValueError, IndexError) as exc:
        return {
            'supported_max_khz': STOCK_CPU_MAX_KHZ,
            'supported_max_mhz': STOCK_CPU_MAX_KHZ / 1000.0,
            'rail_max_mv': None,
            'opp_2091_mv': None,
            'reason': 'CPU rail capability probe unavailable (%s); using stock limit.' % exc,
        }


def write_value(path, value):
    path = Path(path).resolve()
    if path not in ALLOWED:
        raise RuntimeError('refusing non-whitelisted sysfs path: %s' % path)
    fd = os.open(str(path), os.O_WRONLY | os.O_CLOEXEC)
    try:
        os.write(fd, str(value).encode('ascii'))
    finally:
        os.close(fd)


def stock_baseline():
    return {
        'cpu_governor': 'performance',
        'cpu_min': 102000,
        'cpu_max': STOCK_CPU_MAX_KHZ,
        'gpu_governor': 'nvhost_podgov',
        'gpu_min': 76800000,
        'gpu_max': STOCK_GPU_MAX_HZ,
        'emc_max': STOCK_EMC_MAX_HZ,
        'main_ini_sha': sha(MAIN_INI) if MAIN_INI.exists() else None,
    }


def sha(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_baseline():
    global BASELINE_CACHE
    if BASELINE_CACHE is not None:
        return BASELINE_CACHE
    data = read_json(BASELINE)
    if not isinstance(data, dict) or not all(
            key in data for key in ('cpu_governor', 'cpu_min', 'cpu_max',
                                    'gpu_governor', 'gpu_min', 'gpu_max')):
        data = stock_baseline()
        atomic_write_json(BASELINE, data, 0o644)
        say('BASELINE_CREATED', baseline=data)
    BASELINE_CACHE = data
    return data


def boot_env():
    try:
        cmdline = Path('/proc/cmdline').read_text()
    except OSError:
        cmdline = ''
    tokens = {}
    for token in cmdline.split():
        if '=' in token:
            key, value = token.split('=', 1)
            tokens[key] = value
    speedo = lambda key: tokens.get('speedo_tegra210.%s' % key)
    caps = capture()
    cpu_oc = caps['cpu_max'] > STOCK_CPU_MAX_KHZ or caps['cpuinfo_max'] > STOCK_CPU_MAX_KHZ
    gpu_oc = caps['gpu_max'] > STOCK_GPU_MAX_HZ
    ram_oc = (caps['emc_max'] or 0) > STOCK_EMC_MAX_HZ
    return {
        'speedo_sku': speedo('sku_id'),
        'speedo_cspd': speedo('cspd_id'),
        'speedo_sspd': speedo('sspd_id'),
        'speedo_gspd': speedo('gspd_id'),
        'dvfsb': speedo('cspd_id') == '2' or speedo('sspd_id') == '2',
        'gpu_dvfsc': speedo('gspd_id') == '3',
        'cpu_boot_max': caps['cpuinfo_max'],
        'gpu_boot_max': max(caps['gpu_available']) if caps['gpu_available'] else caps['gpu_max'],
        'emc_boot_max': caps['emc_max'],
        'cpu_oc': cpu_oc,
        'gpu_oc': gpu_oc,
        'ram_oc': ram_oc,
    }


def parse_ini(path):
    result = {}
    try:
        for raw in Path(path).read_text(errors='replace').splitlines():
            line = raw.strip()
            if not line or line.startswith('[') or line.startswith(';') or line.startswith('#'):
                continue
            if '=' in line:
                key, value = line.split('=', 1)
                result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def prepared_entries():
    """The one common StegraOS - OC entry.

    `cpu_gpu` is kept as a compatibility alias for the earlier UI protocol;
    it always points at the same common entry.  No separate per-domain entries
    are generated any more.
    """
    result = {'oc': None, 'cpu_gpu': None, 'ram': {}}
    path = INI_DIR / OC_INI_NAME
    try:
        if path.exists():
            data = parse_ini(path)
            if data.get('dvfsb') == '1' and data.get('gpu_dvfsc') == '1':
                entry = {'path': str(path), 'sha256': sha(path), 'keys': data}
                result['oc'] = entry
                result['cpu_gpu'] = entry
                raw = data.get('ram_oc')
                if raw is not None:
                    try:
                        rate = int(raw)
                    except ValueError:
                        rate = None
                    if rate is not None:
                        result['ram'][rate] = entry
    except OSError:
        pass
    return result


def main_ini_status():
    baseline = read_baseline()
    current = sha(MAIN_INI) if MAIN_INI.exists() else None
    return {
        'path': str(MAIN_INI),
        'sha256': current,
        'baseline_sha256': baseline.get('main_ini_sha'),
        'unchanged': bool(current and current == baseline.get('main_ini_sha')),
    }


def applied_state():
    caps = capture()
    baseline = read_baseline()
    cpu_applied = None
    if not (caps['cpu_governor'] == baseline['cpu_governor'] and
            caps['cpu_max'] == baseline['cpu_max']):
        cpu_applied = caps['cpu_max']
    gpu_applied = None
    if not (caps['gpu_governor'] == baseline['gpu_governor'] and
            caps['gpu_max'] == baseline['gpu_max']):
        gpu_applied = caps['gpu_max']
    return {'cpu': cpu_applied, 'gpu': gpu_applied}


def entry_status(domain, item, caps, boot, prepared, applied, cpu_cap=None):
    value = item.get('canonical')
    if not item.get('mapped') or value is None:
        return 'unavailable', item.get('reason') or 'No verified canonical rate.'
    if domain == 'cpu':
        if value > CPU_PROFILE_MAX_KHZ:
            return 'unavailable', 'CPU 2091 profile removed; maximum supported is 1963.5 MHz.'
        supported = (cpu_cap or {}).get('supported_max_khz', STOCK_CPU_MAX_KHZ)
        if value > supported:
            return 'requires_setup', ((cpu_cap or {}).get('reason') or
                                      'CPU OPP is above the current VDD_CPU rail limit.')
        if value <= caps['cpuinfo_max']:
            active = applied['cpu'] == value
            return ('active' if active else 'available',
                    'Current runtime cap.' if active else 'Available now.')
        if value <= LITE_CPU_MAX_KHZ:
            ready = prepared.get('oc') is not None
            return 'restart', ('StegraOS - OC entry is prepared; select it at boot.'
                               if ready else 'Requires the StegraOS - OC boot entry.')
        return 'unavailable', 'Above the Switch Lite boot limit.'
    if domain == 'gpu':
        if value > LITE_GPU_MAX_HZ:
            return 'unavailable', 'Switch Lite (Vali) hard limit is 844 MHz.'
        if value <= max(caps['gpu_available']):
            active = applied['gpu'] == value
            return ('active' if active else 'available',
                    'Current runtime cap.' if active else 'Available now.')
        if value <= LITE_GPU_MAX_HZ:
            ready = prepared.get('oc') is not None
            return 'restart', ('StegraOS - OC entry is prepared; select it at boot.'
                               if ready else 'Requires the StegraOS - OC boot entry.')
        return 'unavailable', 'Above the Switch Lite boot limit.'
    # RAM: catalog values are kHz boot-ceiling requests; runtime EMC is Hz.
    current_emc_khz = (caps['emc_max'] or 0) // 1000
    if value == current_emc_khz:
        return 'active', 'Current boot ceiling; EMC DVFS remains automatic.'
    if value < 1600000:
        return 'unavailable', 'Below the L4T ram_oc boot range; runtime DVFS only.'
    if value in PREPARABLE_RAM_KHZ:
        ready = value in prepared.get('ram', {})
        if value < current_emc_khz:
            return 'restart', ('Lower RAM boot ceiling requires the StegraOS - OC entry.'
                               if not ready else 'StegraOS - OC entry is prepared; select it at boot.')
        return 'restart', ('StegraOS - OC entry is prepared; select it at boot.'
                           if ready else 'Requires the StegraOS - OC boot entry (press A to prepare).')
    if value <= 2133000:
        return 'restart', 'Requires the StegraOS - OC entry; not prepared.'
    return 'requires_setup', ('No validated L4T T210B01 boot entry; OC Suite is not an L4T backend.')


def annotated_catalog(caps, boot, prepared, applied, cpu_cap=None):
    result = {}
    for domain in ('cpu', 'gpu', 'ram'):
        rows = []
        for item in get_catalog()[domain]:
            row = dict(item)
            status, reason = entry_status(domain, item, caps, boot, prepared, applied,
                                          cpu_cap)
            row['status'] = status
            row['reason'] = reason
            row['active'] = bool(status == 'active' and
                                 (domain == 'ram' or applied[domain] == item.get('canonical')))
            rows.append(row)
        result[domain] = rows
    return result


def status(include_thermal=True):
    try:
        caps = capture()
        boot = boot_env()
        prepared = prepared_entries()
        applied = applied_state()
        baseline = read_baseline()
        cpu_cap = cpu_rail_capability(caps)
        result = {
            'ok': True,
            'schema': 2,
            'zram': zram_control.status(),
            'startup_restore': STARTUP_RESTORE,
            'baseline': baseline,
            'boot': boot,
            'cpu_capability': cpu_cap,
            'boot_entries': {
                'prepared': prepared,
                'main': main_ini_status(),
                'oc': str(INI_DIR / OC_INI_NAME),
                'cpu_gpu': str(INI_DIR / OC_INI_NAME),
                'ram_dir': str(INI_DIR),
            },
            'cpu': {
                'governor': caps['cpu_governor'],
                'min': caps['cpu_min'],
                'max': caps['cpu_max'],
                'cur': caps['cpu_cur'],
                'scaling_cur': caps['cpu_scaling_cur'],
                'cpuinfo_max': caps['cpuinfo_max'],
                'available': caps['cpu_available'],
                'unit': 'kHz',
                'applied': applied['cpu'],
            },
            'gpu': {
                'governor': caps['gpu_governor'],
                'min': caps['gpu_min'],
                'max': caps['gpu_max'],
                'cur': caps['gpu_cur'],
                'target': caps['gpu_target'],
                'load': caps['gpu_load'],
                'available': caps['gpu_available'],
                'unit': 'Hz',
                'applied': applied['gpu'],
            },
            'emc': {
                'cur': caps['emc_cur'],
                'min': caps['emc_min'],
                'max': caps['emc_max'],
                'unit': 'Hz',
                'controlled': False,
                'boot_ceiling_khz': (caps['emc_max'] or 0) // 1000,
            },
            'catalog': annotated_catalog(caps, boot, prepared, applied, cpu_cap),
            'applied': applied,
            'limits': {
                'cpu_lite_max_khz': LITE_CPU_MAX_KHZ,
                'gpu_lite_max_hz': LITE_GPU_MAX_HZ,
                'stock_cpu_max_khz': STOCK_CPU_MAX_KHZ,
                'stock_gpu_max_hz': STOCK_GPU_MAX_HZ,
                'stock_emc_max_hz': STOCK_EMC_MAX_HZ,
            },
        }
        if include_thermal:
            result['thermal'] = thermal()
        return result
    except (OSError, ValueError, RuntimeError) as exc:
        return {'ok': False, 'error': str(exc)}


def verify_cpu(target, timeout=2.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = capture()
        if (last['cpu_governor'] == target['governor'] and
                last['cpu_min'] == target['min'] and
                last['cpu_max'] == target['max'] and
                last['cpu_cur'] <= target['max'] + 2000):
            return last
        time.sleep(0.1)
    raise RuntimeError('CPU verification failed: %r' % (last,))


def verify_gpu(target, timeout=2.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = capture()
        if (last['gpu_governor'] == target['governor'] and
                last['gpu_min'] == target['min'] and
                last['gpu_max'] == target['max'] and
                last['gpu_cur'] <= target['max'] + 2000):
            return last
        time.sleep(0.1)
    raise RuntimeError('GPU verification failed: %r' % (last,))


def restore_cpu(before):
    try:
        write_value(CPU_FILES['governor'], before['cpu_governor'])
        write_value(CPU_FILES['max'], before['cpu_max'])
        write_value(CPU_FILES['min'], before['cpu_min'])
        verify_cpu({'governor': before['cpu_governor'], 'min': before['cpu_min'],
                    'max': before['cpu_max']})
        return True
    except (OSError, RuntimeError) as exc:
        say('CPU_RESTORE_FAILED', error=str(exc))
        return False


def restore_gpu(before):
    try:
        write_value(GPU_FILES['governor'], before['gpu_governor'])
        write_value(GPU_FILES['min'], before['gpu_min'])
        write_value(GPU_FILES['max'], before['gpu_max'])
        verify_gpu({'governor': before['gpu_governor'], 'min': before['gpu_min'],
                    'max': before['gpu_max']})
        return True
    except (OSError, RuntimeError) as exc:
        say('GPU_RESTORE_FAILED', error=str(exc))
        return False


def apply_cpu(value):
    caps = capture()
    if value > CPU_PROFILE_MAX_KHZ:
        return {'ok': False, 'status': 'unavailable',
                'reason': 'CPU 2091 profile removed; maximum supported is 1963.5 MHz; no write performed.'}
    cap = cpu_rail_capability(caps)
    if value > cap['supported_max_khz']:
        return {'ok': False, 'status': 'requires_setup',
                'reason': cap.get('reason') or 'CPU OPP is above the VDD_CPU rail limit; no write performed.',
                'capability': cap}
    if value > caps['cpuinfo_max']:
        prepared = prepared_entries()
        return {'ok': False, 'status': 'restart_required',
                'reason': 'Requires the StegraOS - OC boot entry.',
                'prepare': 'oc',
                'prepared': prepared.get('oc') is not None}
    before = {'cpu_governor': caps['cpu_governor'],
              'cpu_min': caps['cpu_min'], 'cpu_max': caps['cpu_max']}
    governor = 'performance' if value >= CPU_PROFILE_MAX_KHZ else 'schedutil'
    try:
        write_value(CPU_FILES['governor'], governor)
        write_value(CPU_FILES['max'], value)
        after = verify_cpu({'governor': governor, 'min': caps['cpu_min'], 'max': value})
    except (OSError, RuntimeError) as exc:
        restored = restore_cpu(before)
        say('CPU_APPLY_ROLLED_BACK', error=str(exc), restored=restored)
        return {'ok': False, 'status': 'error', 'reason': str(exc), 'rolled_back': restored}
    say('CPU_DOMAIN_APPLIED', value=value, governor=governor,
        actual=after['cpu_cur'])
    return {'ok': True, 'status': 'applied', 'domain': 'cpu', 'value': value,
            'actual': after['cpu_cur'], 'governor': governor}


def apply_gpu(value):
    caps = capture()
    if value > LITE_GPU_MAX_HZ:
        return {'ok': False, 'status': 'unavailable',
                'reason': 'Switch Lite (Vali) hard limit is 844 MHz; no write performed.'}
    if value > max(caps['gpu_available']):
        prepared = prepared_entries()
        return {'ok': False, 'status': 'restart_required',
                'reason': 'Requires the StegraOS - OC boot entry (dvfsb=1 / gpu_dvfsc=1).',
                'prepare': 'oc',
                'prepared': prepared.get('oc') is not None}
    before = {'gpu_governor': caps['gpu_governor'],
              'gpu_min': caps['gpu_min'], 'gpu_max': caps['gpu_max']}
    try:
        write_value(GPU_FILES['max'], value)
        after = verify_gpu({'governor': caps['gpu_governor'],
                            'min': caps['gpu_min'], 'max': value})
    except (OSError, RuntimeError) as exc:
        restored = restore_gpu(before)
        say('GPU_APPLY_ROLLED_BACK', error=str(exc), restored=restored)
        return {'ok': False, 'status': 'error', 'reason': str(exc), 'rolled_back': restored}
    say('GPU_DOMAIN_APPLIED', value=value, actual=after['gpu_cur'])
    return {'ok': True, 'status': 'applied', 'domain': 'gpu', 'value': value,
            'actual': after['gpu_cur'], 'governor': caps['gpu_governor']}


def apply_system(domain):
    baseline = read_baseline()
    if domain == 'cpu':
        before = capture()
        ok = restore_cpu({'cpu_governor': baseline['cpu_governor'],
                          'cpu_min': baseline['cpu_min'],
                          'cpu_max': baseline['cpu_max']})
        return {'ok': ok, 'status': 'system' if ok else 'error',
                'domain': 'cpu', 'value': baseline['cpu_max']}
    if domain == 'gpu':
        ok = restore_gpu({'gpu_governor': baseline['gpu_governor'],
                          'gpu_min': baseline['gpu_min'],
                          'gpu_max': baseline['gpu_max']})
        return {'ok': ok, 'status': 'system' if ok else 'error',
                'domain': 'gpu', 'value': baseline['gpu_max']}
    if domain == 'ram':
        caps = capture()
        if (caps['emc_max'] or 0) == baseline['emc_max']:
            return {'ok': True, 'status': 'system', 'domain': 'ram',
                    'value': baseline['emc_max']}
        prepared = prepared_entries()
        ready = 1866000 in prepared.get('ram', {})
        return {'ok': False, 'status': 'restart_required',
                'domain': 'ram', 'value': baseline['emc_max'],
                'reason': 'Stock RAM ceiling requires the StegraOS - OC entry for the stock rate.',
                'prepare': 'oc', 'rate': 1866000, 'prepared': ready}
    return {'ok': False, 'status': 'error', 'reason': 'unknown domain'}


def catalog_entry(domain, value):
    for item in get_catalog()[domain]:
        if item.get('canonical') == value:
            return item
    return None


def apply_domain(domain, value):
    with LOCK_OBJ:
        if domain not in ('cpu', 'gpu', 'ram'):
            return {'ok': False, 'status': 'error', 'reason': 'unknown domain'}
        if value is None:
            return apply_system(domain)
        try:
            value = int(value)
        except (TypeError, ValueError):
            return {'ok': False, 'status': 'error', 'reason': 'value must be an integer'}
        item = catalog_entry(domain, value)
        if item is None or not item.get('mapped'):
            say('DOMAIN_REFUSED', domain=domain, value=value,
                reason='not in canonical catalog')
            return {'ok': False, 'status': 'unavailable',
                    'reason': 'Not a verified canonical rate; no write performed.'}
        if domain == 'cpu':
            result = apply_cpu(value)
        elif domain == 'gpu':
            result = apply_gpu(value)
        else:
            caps = capture()
            current_khz = (caps['emc_max'] or 0) // 1000
            if value == current_khz:
                result = {'ok': True, 'status': 'active', 'domain': 'ram',
                          'value': value, 'reason': 'Current boot ceiling.'}
            elif value in PREPARABLE_RAM_KHZ:
                prepared = prepared_entries()
                ready = value in prepared.get('ram', {})
                result = {'ok': False, 'status': 'restart_required', 'domain': 'ram',
                          'value': value, 'prepare': 'oc', 'rate': value,
                          'prepared': ready,
                          'reason': ('StegraOS - OC entry is prepared; select it at boot.'
                                     if ready else
                                     'Requires the StegraOS - OC entry; press A to prepare.')}
            elif value <= 2133000:
                result = {'ok': False, 'status': 'restart_required', 'domain': 'ram',
                          'value': value, 'prepare': 'oc', 'rate': value,
                          'prepared': False,
                          'reason': 'Requires the StegraOS - OC entry; not prepared.'}
            else:
                result = {'ok': False, 'status': 'requires_setup', 'domain': 'ram',
                          'value': value,
                          'reason': 'No validated L4T T210B01 boot entry; OC Suite is not an L4T backend.'}
        say('DOMAIN_RESULT', domain=domain, value=value,
            status=result.get('status'), ok=result.get('ok'))
        return result


def _common_oc_ram_rate(kind, rate):
    """Choose the RAM boot ceiling stored in the single common OC entry."""
    if kind == 'ram' and rate is not None:
        try:
            value = int(rate)
        except (TypeError, ValueError):
            return None
        return value if value in PREPARABLE_RAM_KHZ else None
    if rate is not None:
        try:
            value = int(rate)
        except (TypeError, ValueError):
            value = None
        if value in PREPARABLE_RAM_KHZ:
            return value
    try:
        choices = saved_choices() or {}
        value = choices.get('ram')
        if value in PREPARABLE_RAM_KHZ:
            return value
    except (OSError, ValueError):
        pass
    try:
        value = (capture()['emc_max'] or 0) // 1000
        if value in PREPARABLE_RAM_KHZ:
            return value
    except (OSError, ValueError):
        pass
    return 2133000


def prepare_boot(kind, rate=None):
    """Create or update the one common StegraOS - OC entry.

    The stock SWITCHDECK-ALPHA.ini is never written.  The common entry carries
    GPU 844 (dvfsb/gpu_dvfsc) and the selected RAM boot ceiling; runtime CPU
    remains capped by the kernel at 1963.5.
    """
    if kind not in ('oc', 'cpu_gpu', 'ram'):
        return {'ok': False, 'status': 'error', 'reason': 'unknown boot entry kind'}
    ram_rate = _common_oc_ram_rate(kind, rate)
    if ram_rate is None:
        return {'ok': False, 'status': 'unavailable',
                'reason': 'RAM rate is not in the prepared L4T allowlist.'}
    body = '\n'.join([
        '[StegraOS - OC]',
        'l4t=1',
        'boot_prefixes=/switchroot/switchdeck-alpha/',
        'id=SWD-ALPHA',
        'emmc=0',
        'r2p_action=bootloader',
        'fbconsole=0',
        'dvfsb=1',
        'gpu_dvfsc=1',
        'ram_oc=%d' % ram_rate,
        '',
    ])
    target = INI_DIR / OC_INI_NAME
    try:
        before_main = sha(MAIN_INI) if MAIN_INI.exists() else None
        if target.exists() and target.read_text(errors='replace') == body:
            return {'ok': True, 'status': 'prepared', 'path': str(target),
                    'sha256': sha(target), 'already': True,
                    'restart_required': True, 'body': body, 'ram_rate': ram_rate}
        BOOT_BACKUP.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.exists():
            (BOOT_BACKUP / (OC_INI_NAME + '.previous')).write_text(
                target.read_text(errors='replace'))
        tmp = INI_DIR / (OC_INI_NAME + '.tmp')
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, 'O_NOFOLLOW', 0), 0o644)
        try:
            os.write(fd, body.encode('utf-8'))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(tmp, target)
        except OSError:
            # Some FAT mounts reject rename; direct rewrite is still guarded by
            # the main-entry hash check below.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                         getattr(os, 'O_NOFOLLOW', 0), 0o644)
            try:
                os.write(fd, body.encode('utf-8'))
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        os.sync()
        after_main = sha(MAIN_INI) if MAIN_INI.exists() else None
        if before_main != after_main:
            raise RuntimeError('main boot entry changed unexpectedly')
        digest = sha(target)
        say('BOOT_ENTRY_PREPARED', kind='oc', rate=ram_rate, path=str(target),
            sha256=digest)
        return {'ok': True, 'status': 'prepared', 'path': str(target),
                'sha256': digest, 'restart_required': True, 'body': body,
                'ram_rate': ram_rate}
    except (OSError, RuntimeError) as exc:
        say('BOOT_ENTRY_REFUSED', kind=kind, rate=rate, error=str(exc))
        return {'ok': False, 'status': 'error', 'reason': str(exc)}


def handle_request(request):
    if not isinstance(request, dict):
        return {'ok': False, 'status': 'error', 'reason': 'request must be an object'}
    op = request.get('op')
    if op == 'status':
        return status()
    if op == 'zram_config':
        try:
            if 'size_gib' not in request: raise ValueError('Missing zram size.')
            return zram_control.save_policy(request['size_gib'])
        except (OSError,ValueError) as exc:
            return {'ok':False,'status':'error','reason':str(exc)}
    if op == 'apply_domain':
        return apply_domain(request.get('domain'), request.get('value'))
    if op == 'prepare_boot':
        return prepare_boot(request.get('kind'), request.get('rate'))
    if op == 'ping':
        return {'ok': True, 'pid': os.getpid()}
    return {'ok': False, 'status': 'error', 'reason': 'unsupported operation'}


def peer_uid(conn):
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_PEERCRED', 17), 12)
        _pid, uid, _gid = struct.unpack('3i', raw)
        return uid
    except OSError:
        return None


def serve():
    RUNTIME.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(RUNTIME, 0, 1000)
    os.chmod(RUNTIME, 0o750)
    try:
        SOCKET.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET))
    os.chmod(SOCKET, 0o660)
    os.chown(SOCKET, 0, 1000)
    server.listen(8)
    server.settimeout(1.0)
    say('READY', socket=str(SOCKET), catalog=len(get_catalog()['cpu']))
    while not STOP:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            conn.settimeout(4.0)
            uid = peer_uid(conn)
            if uid != 1000:
                conn.sendall(b'{"ok":false,"status":"error","reason":"peer uid"}\n')
                continue
            data = b''
            while len(data) < 131072 and b'\n' not in data:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                data += chunk
            try:
                request = json.loads(data.decode('utf-8').strip() or '{}')
            except ValueError:
                response = {'ok': False, 'status': 'error', 'reason': 'invalid json'}
            else:
                response = handle_request(request)
            conn.sendall((json.dumps(response, ensure_ascii=False) + '\n').encode('utf-8'))
        except (OSError, ValueError) as exc:
            say('REQUEST_ERROR', error=str(exc))
        finally:
            conn.close()
    server.close()
    try:
        SOCKET.unlink()
    except FileNotFoundError:
        pass
    say('STOPPED')


def stop(_sig, _frame):
    global STOP
    STOP = True


def main():
    global LOCK_OBJ, STARTUP_RESTORE
    if os.geteuid() != 0:
        raise SystemExit('must run as root')
    import fcntl
    STATE_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    lock_fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('another instance is running')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    current=wait_for_clocks()
    baseline = read_baseline()
    say('BASELINE', baseline=baseline, current=current)
    try:
        STARTUP_RESTORE=restore_saved_policy()
        serve()
    finally:
        applied = applied_state()
        if applied['cpu'] is not None:
            restore_cpu({'cpu_governor': baseline['cpu_governor'],
                         'cpu_min': baseline['cpu_min'],
                         'cpu_max': baseline['cpu_max']})
        if applied['gpu'] is not None:
            restore_gpu({'gpu_governor': baseline['gpu_governor'],
                         'gpu_min': baseline['gpu_min'],
                         'gpu_max': baseline['gpu_max']})
        os.close(lock_fd)


if __name__ == '__main__':
    raise SystemExit(main())
