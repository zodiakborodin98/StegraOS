#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Clock catalog: requested labels, canonical L4T rates and source provenance.

The YAML file is the user's requested catalog, not a hardware allowlist.  This
module maps its MHz labels to the exact Linux rates only where the mapping is
known.  Unmapped labels are kept as explicit rows and are never rounded to a
nearby hardware step.
"""
from pathlib import Path

GROUPS = ('sysclk_original', 'sysclk_oc_extra',
          'oc_suite_ram_extra', 'l4t_mariko_ram')
SOURCE_NAMES = {
    'sysclk_original': 'Original',
    'sysclk_oc_extra': 'sys-clk OC',
    'oc_suite_ram_extra': 'OC Suite',
    'l4t_mariko_ram': 'L4T',
}

# sys-clk labels are integer MHz; the kernel's real rates are in Hz and some
# are not integral MHz.  These are explicit, verified label -> rate mappings.
CPU_MAP = {
    612: 612000, 714: 714000, 816: 816000, 918: 918000,
    1020: 1020000, 1122: 1122000, 1224: 1224000, 1326: 1326000,
    1428: 1428000, 1581: 1581000, 1683: 1683000, 1785: 1785000,
    1887: 1887000, 1963: 1963500, 2091: 2091000,
    2193: 2193000, 2295: 2295000, 2397: 2397000,
}
GPU_MAP = {
    76: 76800000, 153: 153600000, 230: 230400000, 307: 307200000,
    384: 384000000, 460: 460800000, 537: 537600000, 614: 614400000,
    691: 691200000, 768: 768000000, 844: 844800000,
    921: 921600000, 998: 998400000, 1075: 1075200000,
    1152: 1152000000, 1228: 1228800000, 1267: 1267200000,
    1305: 1305600000,
}
# T210B01 rates from the Switchroot/hekate documentation.  These are exact
# bootloader values in kHz; the UI displays them in MHz.
L4T_RAM_KHZ = [
    1600000, 1866000, 2133000, 2166000, 2200000, 2233000, 2266000,
    2300000, 2333000, 2366000, 2400000, 2433000, 2466000, 2500000,
    2533000, 2566000, 2600000, 2633000, 2666000, 2700000, 2733000,
    2766000, 2800000, 2833000, 2866000, 2900000, 2933000, 2966000,
    3000000, 3033000, 3066000, 3100000, 3133000, 3166000, 3200000,
]
RAM_MAP = {khz // 1000: khz for khz in L4T_RAM_KHZ}
RAM_MAP[1331] = 1331200          # Original runtime rate
RAM_MAP[1600] = 1600000
RAM_MAP[1866] = 1866000
RAM_MAP[2400] = 2400000          # OC Suite label with an exact L4T rate
RAM_MAP[3200] = 3200000


def parse_yaml(path):
    """Parse the small fixed catalog without an external YAML dependency."""
    groups = {}
    current = None
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped == 'clock_tables_mhz:':
            continue
        if line.startswith('  ') and not line.startswith('    '):
            key = stripped[:-1] if stripped.endswith(':') else stripped
            if key:
                current = key
                groups.setdefault(current, {})
            continue
        if line.startswith('    ') and ':' in stripped and current:
            key, rest = stripped.split(':', 1)
            rest = rest.strip()
            if rest.startswith('[') and rest.endswith(']'):
                values = []
                for item in rest[1:-1].split(','):
                    item = item.strip()
                    if item:
                        values.append(int(item))
                groups[current][key.strip()] = values
    for group in GROUPS:
        groups.setdefault(group, {})
    return groups


def display_value(value, unit):
    if unit == 'kHz':
        mhz = value / 1000.0
    else:
        mhz = value / 1000000.0
    text = ('%.1f' % mhz).rstrip('0').rstrip('.')
    return text + ' MHz'


def label_text(label):
    return '%d MHz' % label


def _add(result, domain, value, label, source, mapped):
    unit = 'Hz' if domain == 'gpu' else 'kHz'
    if mapped:
        key = ('canonical', domain, value)
    else:
        key = ('raw', domain, label)
    item = result.setdefault(key, {
        'domain': domain,
        'canonical': value if mapped else None,
        'unit': unit,
        'display': display_value(value, unit) if mapped else label_text(label),
        'requested': [],
        'sources': [],
        'mapped': mapped,
        'reason': None if mapped else
        'No verified L4T canonical rate; the label is not rounded.',
    })
    if label not in item['requested']:
        item['requested'].append(label)
    if source not in item['sources']:
        item['sources'].append(source)
    return item


def build_catalog(path):
    groups = parse_yaml(path)
    result = {'cpu': {}, 'gpu': {}, 'ram': {}}
    for group in GROUPS:
        data = groups.get(group, {})
        source = SOURCE_NAMES[group]
        for domain, mapping in (('cpu', CPU_MAP), ('gpu', GPU_MAP), ('ram', RAM_MAP)):
            for label in data.get(domain, []):
                value = mapping.get(label)
                _add(result[domain], domain, value, label, source, value is not None)
    out = {}
    for domain in ('cpu', 'gpu', 'ram'):
        mapped = [v for v in result[domain].values() if v['mapped']]
        raw = [v for v in result[domain].values() if not v['mapped']]
        mapped.sort(key=lambda item: item['canonical'])
        raw.sort(key=lambda item: (item['requested'][0] if item['requested'] else 0))
        out[domain] = mapped + raw
    return out


def load_catalog(path):
    return build_catalog(path)


if __name__ == '__main__':
    import json
    import sys
    data = load_catalog(sys.argv[1] if len(sys.argv) > 1 else
                        str(Path(__file__).with_name('clock-tables-mhz.yaml')))
    print(json.dumps(data, indent=2, sort_keys=True))
