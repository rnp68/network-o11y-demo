#!/usr/bin/env python3
"""Retarget the AWS-authored net-o11y dashboards to the local ktranslate/OTLP schema.

The dashboards under ../../grafana/dashboards/ were authored for the AWS / EKS
path (Grafana Cloud SNMP + gNMI integrations). Their metric names and labels do
NOT match what the local `local/` deployment ships via ktranslate + Alloy OTLP,
so every panel is empty against a local stack until retargeted.

This reads the AWS source dashboards and writes local-schema variants to
../dashboards/ (local/dashboards/). It does NOT modify the AWS originals, so the
AWS / EKS path is unaffected.

Schema mapping (local, verified 2026-07):
  SNMP   metrics carry a `kentik_snmp_` prefix (ifHCInOctets -> kentik_snmp_ifHCInOctets,
         ifOperStatus -> kentik_snmp_if_OperStatus, sysUpTime -> kentik_snmp_Uptime);
         device is the `device_name` label (not `source`/`job`); interface is
         `if_Description` (e.g. ethernet-1/1); a single job `ktranslate-snmp-srl-<host>`.
         AWS `job=~"integrations/snmp/..."` selectors -> `device_name` selectors.
  gNMI   metric names match as-authored but carry `job="network-topology-exporter"`
         (NOT `job="gnmic"`); device is the `source` label. Only bgp_neighbors +
         lldp are subscribed locally, so gNMI CPU/interface metrics do not exist —
         `srl_cpu_*` / `srl_iface_*` / `srl_memory_*` remap to kentik_snmp_ equivalents.
  Flow   network_io_by_flow_bytes (unchanged).
  Loki   device syslog lives under {service_name="ktranslate"} (ktranslate --tee_logs;
         no hostname/severity stream labels).

Usage:
    python3 local/scripts/retarget-dashboards-local.py
    # then import (with gcx OAuth, or GRAFANA_URL+GRAFANA_TOKEN):
    #   gcx --context <ctx> api /api/dashboards/db -d @<wrapped-payload>
"""
import json
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "grafana", "dashboards")
DST = os.path.join(ROOT, "local", "dashboards")

# net-o11y dashboards that target fabric telemetry (the linux-compliance-* set is
# out of scope: it has no equivalent on the local network lab).
NAMES = [
    "bgp-status", "device-details", "interface-health",
    "network-topology", "traffic-flows", "traffic-sankey",
]


def snmp(s: str) -> str:
    # memory utilization: no local gauge -> compute from Used / (Used + Available)
    s = re.sub(
        r"srl_memory_utilization\{([^}]*)\}",
        r"(100 * sum by (device_name) (kentik_snmp_MemoryUsed{\1}) "
        r"/ (sum by (device_name) (kentik_snmp_MemoryUsed{\1}) "
        r"+ sum by (device_name) (kentik_snmp_MemoryAvailable{\1})))",
        s,
    )
    # gNMI metrics that do not exist locally -> SNMP equivalents
    for a, b in [
        ("srl_cpu_total_average_1", "kentik_snmp_CPU"),
        ("srl_cpu_total_average_5", "kentik_snmp_CPU"),
        ("srl_memory_free", "kentik_snmp_MemoryAvailable"),
        ("srl_iface_in_octets", "kentik_snmp_ifHCInOctets"),
        ("srl_iface_out_octets", "kentik_snmp_ifHCOutOctets"),
        ("srl_iface_in_packets", "kentik_snmp_ifHCInUcastPkts"),
        ("srl_iface_out_packets", "kentik_snmp_ifHCOutUcastPkts"),
        ("srl_iface_in_discarded_packets", "kentik_snmp_ifInDiscards"),
        ("srl_iface_in_error_packets", "kentik_snmp_ifInErrors"),
    ]:
        s = s.replace(a, b)
    # bare SNMP metric names -> kentik_snmp_ prefixed
    s = re.sub(
        r"(?<![\w])if(HCInOctets|HCOutOctets|HCInUcastPkts|HCOutUcastPkts|"
        r"HCInMulticastPkts|HCOutMulticastPkts|HCInBroadcastPkts|HCOutBroadcastPkts|"
        r"InErrors|OutErrors|InDiscards|OutDiscards)\b",
        r"kentik_snmp_if\1",
        s,
    )
    s = re.sub(r"(?<![\w])ifOperStatus\b", "kentik_snmp_if_OperStatus", s)
    s = re.sub(r"(?<![\w])ifAdminStatus\b", "kentik_snmp_if_AdminStatus", s)
    s = re.sub(r"\bsysUpTime\b", "kentik_snmp_Uptime", s)
    s = re.sub(r"\bsysDescr\b", "kentik_snmp_Uptime", s)
    # Loki syslog panels -> local ktranslate teed logs
    s = re.sub(r'job="syslog/srl",\s*hostname=~"[^"]*"', 'service_name="ktranslate"', s)
    s = re.sub(r'job="syslog/srl"', 'service_name="ktranslate"', s)
    s = re.sub(r'\|\s*severity\s*=~\s*"[^"]*"', '|~ "(?i)warn|error|crit"', s)
    # label renames
    s = re.sub(r"\binterface_name\b", "if_Description", s)
    s = re.sub(r"\bifDescr\b", "if_Description", s)
    s = re.sub(r"\bifName\b", "if_interface_name", s)
    # AWS integration job selectors -> device_name
    s = re.sub(r'job=~?"integrations/snmp/[^"]*\$device[^"]*"', 'device_name=~"$device"', s)
    s = re.sub(r'job=~?"integrations/snmp/[^"]*"', 'device_name=~".+"', s)
    s = re.sub(r'\s*,?\s*job=~?"integrations/gnmi"', "", s)
    # device label + label_values extraction
    s = re.sub(r"\bsource\b", "device_name", s)
    s = re.sub(r"\bby \(job\)", "by (device_name)", s)
    s = re.sub(r"\}, job\)", "}, device_name)", s)
    # selector cleanup
    s = s.replace("{, ", "{").replace("{,", "{")
    s = re.sub(r",\s*\}", "}", s)
    return s


def gnmi(s: str) -> str:
    return s.replace('job="gnmic"', 'job="network-topology-exporter"') \
            .replace('job=~"gnmic"', 'job=~"network-topology-exporter"')


def xform(v):
    if isinstance(v, str):
        return gnmi(v) if "gnmi_" in v else snmp(v)
    if isinstance(v, dict):
        return {k: xform(x) for k, x in v.items()}
    if isinstance(v, list):
        return [xform(x) for x in v]
    return v


def fix_variable_regex(dash):
    """Template variables extracted a short name from the AWS `job` label via a
    `regex: integrations/snmp/(.*)` field. Locally the variable already returns
    clean device_name values (spine1/leaf1/leaf2), so that regex now filters
    everything out -> empty variable -> blank dashboard. Clear it."""
    for var in dash.get("templating", {}).get("list", []):
        rx = var.get("regex")
        if isinstance(rx, str) and "integrations/snmp" in rx:
            var["regex"] = ""
    return dash


# --- local ContainerLab Clos links: (src_dev, src_if, dst_dev, dst_if) ---
LINKS = [
    ("spine1", "e1-1", "leaf1", "e1-49"),
    ("spine1", "e1-2", "leaf2", "e1-49"),
    ("leaf1", "e1-1", "client1", "eth1"),
    ("leaf2", "e1-1", "client2", "eth1"),
]
_NODES = {"spine1": (400, 70), "leaf1": (250, 250), "leaf2": (550, 250),
          "client1": (250, 430), "client2": (550, 430)}
_NW, _NH = 120, 44
_NODE_FILL = {"spine1": "#2b5f9e", "leaf1": "#2f7d4f", "leaf2": "#2f7d4f",
              "client1": "#5a4b8a", "client2": "#5a4b8a"}


def _fabric_svg():
    """Inline SVG of the local fabric. Element ids `cell-<key>` / `cell-link_id:<key>`
    bind to the flow-panel config below; each link cell wraps a <text> the panel fills
    with the bps value."""
    W, H = 800, 520
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
         f'height="{H}" font-family="sans-serif">',
         f'<rect x="0" y="0" width="{W}" height="{H}" fill="#0d1117"/>']
    for s, si, d, di in LINKS:
        key = f"{s}:{si}:{d}:{di}"
        sx, sy = _NODES[s]; dx, dy = _NODES[d]
        x1, y1 = sx, sy + _NH // 2; x2, y2 = dx, dy - _NH // 2
        mx, my = (x1 + x2) // 2, (y1 + y2) // 2
        p.append(f'<g id="cell-link_id:{key}" stroke="#bec8d2">'
                 f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke-width="8" stroke-linecap="round"/>'
                 f'<text x="{mx}" y="{my - 8}" font-size="12" fill="#e6edf3" '
                 f'text-anchor="middle" stroke="none">0</text></g>')
        ex, ey = int(x1 + (x2 - x1) * 0.12), int(y1 + (y2 - y1) * 0.12)
        p.append(f'<rect id="cell-{key}" x="{ex - 6}" y="{ey - 6}" width="12" height="12" '
                 f'rx="2" fill="#4BDD33" stroke="#0d1117" stroke-width="1"/>')
        p.append(f'<text x="{ex + 10}" y="{ey + 4}" font-size="11" fill="#8b949e" stroke="none">{si}</text>')
    for n, (cx, cy) in _NODES.items():
        x, y = cx - _NW // 2, cy - _NH // 2
        p.append(f'<rect x="{x}" y="{y}" width="{_NW}" height="{_NH}" rx="7" fill="{_NODE_FILL[n]}" '
                 f'stroke="#c9d1d9" stroke-width="1.5"/>'
                 f'<text x="{cx}" y="{cy + 5}" font-size="16" fill="#ffffff" text-anchor="middle">{n}</text>')
    p.append("</svg>")
    return "".join(p)


def _fabric_config():
    """andrewbmchugh-flow-panel config. dataRefs match the panel's query legends
    (oper-state:<dev>:<if>, <dev>:<if>:out)."""
    lines = ["---", "cellIdPreamble: cell-", "cells:"]
    for s, si, d, di in LINKS:
        key = f"{s}:{si}:{d}:{di}"
        lines += [
            f"  {key}:", f"    dataRef: oper-state:{s}:{si}",
            "    fillColor:", "      thresholds:",
            "        - {color: '#FF3154', level: 0}",
            "        - {color: '#4BDD33', level: 1}",
            f"  link_id:{key}:", f"    dataRef: {s}:{si}:out",
            "    label:", "      separator: replace", "      units: bps", "      decimalPoints: 1",
            "    strokeColor:", "      thresholds:",
            "        - {color: '#bec8d2', level: 0}",
            "        - {color: '#4BDD33', level: 200000}",
            "        - {color: '#FFFF00', level: 1000000}",
            "        - {color: '#FF8000', level: 5000000}",
            "        - {color: '#FF3154', level: 20000000}",
        ]
    return "\n".join(lines) + "\n"


def fix_fabric_map(dash):
    """The Fabric Map flow-panel shipped with svg/panelConfig pointing at a deleted
    upstream branch (srl-telemetry-lab@flow_panel -> 404). Replace with inline
    local-topology SVG + config."""
    for pan in dash.get("panels", []):
        if pan.get("title") == "Fabric Map" and str(pan.get("type", "")).startswith("andrewbmchugh"):
            pan.setdefault("options", {})
            pan["options"]["svg"] = _fabric_svg()
            pan["options"]["panelConfig"] = _fabric_config()
            # siteConfig also pointed at the deleted upstream branch (404 -> the
            # plugin fetched a GitHub HTML page and threw "Extra content at the end
            # of the document"). It is optional; clear it so nothing is fetched.
            pan["options"]["siteConfig"] = ""
    return dash


# Sankey queries: emit source+destination for the real local links (the organize
# transform maps source->Source, destination->Destination, Value->bps).
_S2L = ('sum by (source, destination) (label_replace(label_replace(label_replace('
        'rate(kentik_snmp_ifHCOutOctets{device_name="spine1", if_Description=~"ethernet-1/[12]"}[$__rate_interval]) * 8, '
        '"source","$1","device_name","(.*)"), "destination","leaf1","if_Description","ethernet-1/1"), '
        '"destination","leaf2","if_Description","ethernet-1/2"))')
_L2S = ('sum by (source, destination) (label_replace(label_replace('
        'rate(kentik_snmp_ifHCOutOctets{device_name=~"leaf[12]", if_Description="ethernet-1/49"}[$__rate_interval]) * 8, '
        '"source","$1","device_name","(.*)"), "destination","spine1","if_Description","ethernet-1/49"))')
_SANKEY = {"Spine → Leaf": _S2L, "Leaf → Spine": _L2S, "Spine ↔ Leaf (Aggregate)": f"({_S2L}) or ({_L2S})"}


def fix_sankey(dash):
    for pan in dash.get("panels", []):
        if pan.get("title") in _SANKEY and pan.get("targets"):
            pan["targets"][0]["expr"] = _SANKEY[pan["title"]]
            pan["targets"][0].update({"format": "table", "instant": True, "range": False})
    return dash


def pin_datasource(dash):
    """Default the datasource variable to grafanacloud-prom (else Grafana/renderer can
    pick another Prometheus datasource and the panels look empty)."""
    for var in dash.get("templating", {}).get("list", []):
        if var.get("name") == "datasource":
            var["current"] = {"selected": True, "text": "grafanacloud-prom", "value": "grafanacloud-prom"}
    return dash


def main():
    os.makedirs(DST, exist_ok=True)
    for name in NAMES:
        src = os.path.join(SRC, f"{name}.json")
        with open(src, encoding="utf-8") as f:
            dash = json.load(f)
        dash = xform(dash)
        dash = fix_variable_regex(dash)
        dash = pin_datasource(dash)
        if name == "traffic-sankey":
            dash = fix_sankey(dash)
        if name == "network-topology":
            dash = fix_fabric_map(dash)
        out = os.path.join(DST, f"{name}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(dash, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"wrote {os.path.relpath(out, ROOT)}")


if __name__ == "__main__":
    main()
