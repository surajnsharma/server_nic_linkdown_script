#!/usr/bin/env python3
"""
amber_summarize.py

Summarize NVIDIA amBER CSV output into a human-readable link health report.

Usage:
    python3 amber_summarize.py amber_*.csv
"""

import argparse
import csv
import os
import subprocess
import math
from datetime import datetime
from typing import Dict, Any, List, Optional, TextIO


# -------------------- Helpers -------------------- #

def clean_line(line: bytes) -> str:
    """
    Decode a line from the CSV and remove NUL bytes.
    amBER CSVs sometimes contain embedded NULs which break csv.reader.
    """
    line = line.replace(b"\x00", b" ")
    try:
        return line.decode("utf-8", errors="replace")
    except Exception:
        return line.decode("latin1", errors="replace")


def read_csv_safely(path: str) -> List[Dict[str, Any]]:
    """
    Read a CSV file that may contain NUL bytes or odd encodings.
    Returns a list of dict rows.
    """
    with open(path, "rb") as f:
        cleaned_lines = [clean_line(l) for l in f]

    reader = csv.DictReader(cleaned_lines)
    rows: List[Dict[str, Any]] = []
    for row in reader:
        # Skip empty lines
        if not any(str(v).strip() for v in row.values()):
            continue
        rows.append(row)
    return rows


def safe_get(row: Dict[str, Any], key: str, default: str = "N/A") -> str:
    """Return row[key] if present and non-empty, else default."""
    v = row.get(key, "")
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def safe_float(val: str, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(val)
    except Exception:
        return default


def scientific_str(val: Optional[float]) -> str:
    """
    Compact scientific notation for floats; returns 'N/A' if val is None.
    """
    if val is None:
        return "N/A"
    if val == 0:
        return "0"
    exp = int(math.floor(math.log10(abs(val))))
    mant = val / (10 ** exp)
    return f"{mant:.2f}e{exp:+d}"


def format_large_number(num: int) -> str:
    """
    Format large numbers in human-readable format (e.g., 30B, 1.5M).
    """
    if num < 1000:
        return str(num)
    elif num < 1_000_000:
        return f"{num / 1000:.1f}K"
    elif num < 1_000_000_000:
        return f"{num / 1_000_000:.1f}M"
    else:
        return f"{num / 1_000_000_000:.1f}B"


def summarize_histogram(row: Dict[str, Any]) -> str:
    """
    Summarize hist0..hist15 into a short text line:
    - total corrections
    - first few bins
    - highest bin with non-zero count
    """
    counts: List[int] = []
    for i in range(16):
        key = f"hist{i}"
        v = safe_get(row, key, "0")
        try:
            counts.append(int(v))
        except Exception:
            counts.append(0)

    total = sum(counts)
    nonzero_bins = [i for i, c in enumerate(counts) if c > 0]

    if total == 0 or not nonzero_bins:
        return "No FEC histogram data (all bins are zero)."

    first_bins = ", ".join(format_large_number(c) for c in counts[:4])
    max_bin = max(nonzero_bins)
    total_formatted = format_large_number(total)
    return (
        f"Total corrections: {total_formatted} ({total:,}) across 16 bins. "
        f"First bins: [{first_bins}, ...]. "
        f"Highest nonzero bin index: {max_bin}."
    )


def classify_link_health(row: Dict[str, Any]) -> str:
    """
    Rough heuristic based on Raw_BER and Effective_BER.
    This is only a human-friendly hint, NOT vendor-official logic.
    """
    raw_ber_str = safe_get(row, "Raw_BER", "")
    eff_ber_str = safe_get(row, "Effective_BER", "")

    raw_ber = safe_float(raw_ber_str)
    eff_ber = safe_float(eff_ber_str)

    if raw_ber is None and eff_ber is None:
        return "Unknown: BER fields missing or unparsable – check full CSV."

    # Healthy: low raw BER and very low effective BER
    if (raw_ber is None or raw_ber <= 1e-8) and (eff_ber is None or eff_ber <= 1e-12):
        return "Healthy: very low BER; FEC overhead looks minimal."

    # Correctable but noisy
    if raw_ber is not None and raw_ber > 1e-8 and (eff_ber is None or eff_ber <= 1e-12):
        return (
            "Correctable but somewhat noisy: FEC is doing real work; "
            "worth monitoring if issues persist."
        )

    # Effective BER also non-trivial
    if eff_ber is not None and eff_ber > 1e-12:
        return (
            "Potentially marginal link: effective BER is non-negligible; "
            "consider checking optics, cable, and host configuration."
        )

    return "Intermediate: some corrections present; suggest deeper review of histograms and SNR."


def log_msg(msg: str, logf: Optional[TextIO]) -> None:
    """Print to stdout and also write to log file if provided."""
    print(msg)
    if logf is not None:
        logf.write(msg + "\n")


def get_local_if_map() -> Dict[str, Dict[str, str]]:
    """
    Build a mapping:
        mac_str (e.g. '9c:63:c0:03:58:d0') ->
            {'ifname': 'enp180s0f0np0', 'state': 'UP'}

    Uses `ip -o link`. Best-effort; returns {} if ip is unavailable.
    """
    try:
        out = subprocess.check_output(["ip", "-o", "link"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return {}

    mapping: Dict[str, Dict[str, str]] = {}
    
    # Join lines that are split by backslash continuation
    full_lines = []
    current_line = ""
    for line in out.splitlines():
        line = line.rstrip()
        # Remove backslash continuation and join
        if line.endswith("\\"):
            current_line += line[:-1] + " "
        else:
            current_line += line
            if current_line.strip():
                full_lines.append(current_line.strip())
            current_line = ""
    
    # Process each complete line
    for line in full_lines:
        if not line:
            continue

        current_ifname = None
        current_state = "UNKNOWN"
        mac = None

        # Example format (all on one line after joining):
        # 5: enp13s0f0np0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9000 qdisc mq state UP ... link/ether c4:70:bd:31:ec:38 brd ff:ff:ff:ff:ff:ff
        
        # Check if this is a new interface line (starts with number:)
        if ":" in line and line[0].isdigit():
            parts = line.split(":", 2)
            if len(parts) >= 2:
                current_ifname = parts[1].strip()
                
                # Extract state from angle brackets: <BROADCAST,MULTICAST,UP,LOWER_UP>
                if "<" in line and ">" in line:
                    state_part = line[line.find("<") + 1:line.find(">")]
                    states = [s.strip() for s in state_part.split(",")]
                    if "UP" in states or "LOWER_UP" in states:
                        current_state = "UP"
                    elif "DOWN" in states:
                        current_state = "DOWN"
                    else:
                        current_state = "UNKNOWN"
                
                # Also check for explicit state= in the line
                if "state" in line.lower():
                    tokens = line.split()
                    try:
                        state_idx = tokens.index("state")
                        if state_idx + 1 < len(tokens):
                            explicit_state = tokens[state_idx + 1].upper()
                            if explicit_state in ["UP", "DOWN", "UNKNOWN"]:
                                current_state = explicit_state
                    except (ValueError, IndexError):
                        pass
        
        # Check for MAC address (link/ether) - can be on same line or continuation
        if "link/ether" in line.lower():
            tokens = line.split()
            try:
                # Find link/ether token
                for i, token in enumerate(tokens):
                    if token.lower() == "link/ether" and i + 1 < len(tokens):
                        mac = tokens[i + 1].lower()
                        # Remove broadcast address suffix if present
                        if "/" in mac:
                            mac = mac.split("/")[0]
                        break
                
                if current_ifname and mac:
                    mapping[mac] = {"ifname": current_ifname, "state": current_state}
            except (ValueError, IndexError):
                continue

    return mapping


def mac_from_amber_hex(amber_mac: str) -> Optional[str]:
    """
    Convert amBER MAC string like '0x9c63c00358d0' to '9c:63:c0:03:58:d0'.
    Returns None if format is unexpected.
    """
    s = amber_mac.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 12 or any(c not in "0123456789abcdef" for c in s):
        return None
    return ":".join(s[i:i + 2] for i in range(0, 12, 2))


# -------------------- Reporting -------------------- #

def summarize_row(
    row: Dict[str, Any],
    filename: str,
    idx: int,
    logf: TextIO,
    if_map: Dict[str, Dict[str, str]],
) -> None:
    """
    Print and log a human-readable summary for one CSV row.
    """

    def both(msg: str = "") -> None:
        log_msg(msg, logf)

    # Get timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    both("=" * 80)
    both(f"amBER Link Health Report")
    both(f"Generated: {timestamp}")
    both(f"File: {filename}")
    both(f"Row: {idx}")
    both("=" * 80)

    # Link / protocol
    port              = safe_get(row, "Port_Number")
    mac_hex           = safe_get(row, "MAC_Address")
    mac_addr          = mac_from_amber_hex(mac_hex) or "N/A"
    protocol          = safe_get(row, "Protocol")
    speed_gbps        = safe_get(row, "Speed_[Gb/s]")
    active_fec        = safe_get(row, "Active_FEC")
    link_down         = safe_get(row, "Link_Down")
    link_down_gb_host = safe_get(row, "Link_Down_GB_host")
    link_down_gb_line = safe_get(row, "Link_Down_GB_line")
    time_since_clear  = safe_get(row, "Time_since_last_clear_[Min]")

    # Map MAC -> local interface (if running on same host)
    host_if = "N/A"
    host_if_state = "N/A"
    if mac_addr != "N/A":
        info = if_map.get(mac_addr.lower())
        if info:
            host_if = info.get("ifname", "N/A")
            host_if_state = info.get("state", "N/A")

    both("\n[Link / Protocol]")
    both(f"  Port                        : {port}")
    both(f"  MAC Address (amBER)         : {mac_hex}")
    both(f"  MAC Address (parsed)        : {mac_addr}")
    both(f"  Host interface (if found)   : {host_if} (state={host_if_state})")
    both(f"  Protocol                    : {protocol}")
    both(f"  Speed [Gb/s]                : {speed_gbps}")
    both(f"  Active FEC                  : {active_fec}")
    both(f"  Link Down Count             : {link_down}")
    both(f"  Link Down (GB host / line)  : {link_down_gb_host} / {link_down_gb_line}")
    both(f"  Time since last clear [min] : {time_since_clear}")

    # BER / FEC
    raw_ber_str = safe_get(row, "Raw_BER")
    eff_ber_str = safe_get(row, "Effective_BER")
    raw_ber_val = safe_float(raw_ber_str)
    eff_ber_val = safe_float(eff_ber_str)

    raw_ber_lane0 = safe_get(row, "Raw_BER_lane0")
    raw_ber_lane1 = safe_get(row, "Raw_BER_lane1")
    raw_ber_lane2 = safe_get(row, "Raw_BER_lane2")
    raw_ber_lane3 = safe_get(row, "Raw_BER_lane3")

    both("\n[BER / FEC Metrics]")
    both(f"  Raw BER lanes 0–3           : {raw_ber_lane0}, {raw_ber_lane1}, {raw_ber_lane2}, {raw_ber_lane3}")
    both(
        f"  Raw BER (aggregate)         : {raw_ber_str} "
        f"({scientific_str(raw_ber_val)})"
    )
    both(
        f"  Effective BER               : {eff_ber_str} "
        f"({scientific_str(eff_ber_val)})"
    )

    hist_summary = summarize_histogram(row)
    both(f"  FEC Histogram summary       : {hist_summary}")
    
    # Full FEC Histogram breakdown
    both("\n  [Detailed FEC Histogram (all 16 bins)]")
    hist_counts: List[int] = []
    for i in range(16):
        key = f"hist{i}"
        v = safe_get(row, key, "0")
        try:
            count = int(v)
            hist_counts.append(count)
        except Exception:
            hist_counts.append(0)
    
    total_corrections = sum(hist_counts)
    if total_corrections > 0:
        for i, count in enumerate(hist_counts):
            percentage = (count / total_corrections * 100) if total_corrections > 0 else 0
            both(f"    Bin {i:2d}: {format_large_number(count):>8s} ({count:>15,}) - {percentage:5.2f}%")
    else:
        both("    All bins are zero (no FEC corrections)")

    # SNR (if present)
    snr_media = [safe_get(row, f"snr_media_lane{i}", "N/A") for i in range(4)]
    snr_host  = [safe_get(row, f"snr_host_lane{i}", "N/A") for i in range(4)]

    both("\n[SNR (if available)]")
    both(f"  Media lanes 0–3             : {', '.join(snr_media)}")
    both(f"  Host  lanes 0–3             : {', '.join(snr_host)}")

    # Cable / module
    cable_pn      = safe_get(row, "Cable_PN")
    cable_sn      = safe_get(row, "Cable_SN")
    cable_tech    = safe_get(row, "cable_technology")
    cable_type    = safe_get(row, "cable_type")
    cable_vendor  = safe_get(row, "cable_vendor")
    cable_length  = safe_get(row, "cable_length")
    vendor_name   = safe_get(row, "vendor_name")
    module_temp   = safe_get(row, "Module_Temperature")
    module_vcc    = safe_get(row, "Module_Voltage")

    vendor_display = cable_vendor if cable_vendor != "N/A" else vendor_name

    both("\n[Cable / Module]")
    both(f"  Cable PN                    : {cable_pn}")
    both(f"  Cable SN                    : {cable_sn}")
    both(f"  Cable technology            : {cable_tech}")
    both(f"  Cable type                  : {cable_type}")
    both(f"  Vendor                      : {vendor_display}")
    both(f"  Length                      : {cable_length}")
    both(f"  Module temperature          : {module_temp}")
    both(f"  Module voltage              : {module_vcc}")

    # Link events / recovery
    succ_recovery  = safe_get(row, "successful_recovery_events")
    total_recovery = safe_get(row, "total_successful_recovery_events")
    unintent_down  = safe_get(row, "unintentional_link_down_events")
    intent_down    = safe_get(row, "intentional_link_down_events")
    last_down_reason = safe_get(row, "local_reason_opcode")
    down_blame       = safe_get(row, "down_blame")

    both("\n[Link Events / Recovery]")
    both(f"  Successful recovery events  : {succ_recovery}")
    both(f"  Total successful recoveries : {total_recovery}")
    both(f"  Unintentional link-downs    : {unintent_down}")
    both(f"  Intentional link-downs      : {intent_down}")
    both(f"  Last down blame             : {down_blame}")
    both(f"  Last local reason opcode    : {last_down_reason}")

    # Verdict
    verdict = classify_link_health(row)
    both("\n" + "=" * 80)
    both("[SUMMARY / VERDICT]")
    both("=" * 80)
    both(f"  Status: {verdict}")
    both("")
    both("  Quick Reference:")
    both(f"    • Port: {port}")
    both(f"    • Interface: {host_if} ({host_if_state})")
    both(f"    • Speed: {speed_gbps} Gb/s")
    both(f"    • Raw BER: {scientific_str(raw_ber_val)}")
    both(f"    • Effective BER: {scientific_str(eff_ber_val)}")
    both(f"    • Link Downs: {link_down}")
    both("")

    # One-line headline for grep-friendly logs
    both("[Grep-Friendly Headline]")
    both(
        "  "
        f"PORT={port} "
        f"IF={host_if} IF_STATE={host_if_state} "
        f"MAC={mac_addr} SPEED={speed_gbps}G "
        f"LINK_DOWNS={link_down} "
        f"RAW_BER={scientific_str(raw_ber_val)} "
        f"EFF_BER={scientific_str(eff_ber_val)} "
        f"VERDICT='{verdict}'"
    )
    # Additional Statistics and Calculations
    both("\n" + "=" * 80)
    both("[Additional Statistics]")
    both("=" * 80)
    
    # Calculate FEC correction rate if time is available
    time_min = safe_float(time_since_clear)
    if time_min and time_min > 0 and total_corrections > 0:
        corrections_per_min = total_corrections / time_min
        corrections_per_sec = corrections_per_min / 60
        both(f"  FEC Correction Rate        : {format_large_number(int(corrections_per_sec))}/sec ({corrections_per_sec:,.0f}/sec)")
        both(f"  FEC Correction Rate        : {format_large_number(int(corrections_per_min))}/min ({corrections_per_min:,.0f}/min)")
    
    # Calculate link uptime percentage if available
    if link_down != "N/A" and time_min:
        try:
            down_count = int(link_down)
            if time_min > 0:
                # Rough estimate: assume each down is brief (1 second)
                estimated_downtime_sec = down_count
                uptime_percentage = ((time_min * 60 - estimated_downtime_sec) / (time_min * 60)) * 100
                both(f"  Estimated Uptime          : {uptime_percentage:.4f}% (based on {down_count} link downs)")
        except Exception:
            pass
    
    # BER Analysis
    if raw_ber_val is not None:
        both(f"  Raw BER Analysis            : {raw_ber_val:.2e}")
        if raw_ber_val > 1e-6:
            both("    ⚠️  WARNING: Very high raw BER - link may be unstable")
        elif raw_ber_val > 1e-8:
            both("    ⚠️  CAUTION: Elevated raw BER - monitor closely")
        else:
            both("    ✓ Raw BER is within acceptable range")
    
    if eff_ber_val is not None and eff_ber_val < 1e-200:  # Handle very small numbers
        both(f"  Effective BER Analysis      : {eff_ber_val:.2e}")
        if eff_ber_val > 1e-12:
            both("    ⚠️  WARNING: Non-negligible effective BER detected")
        else:
            both("    ✓ Effective BER is excellent")
    
    # Module Health Indicators
    if module_temp != "N/A":
        try:
            temp = float(module_temp)
            both(f"  Module Temperature          : {temp}°C")
            if temp > 70:
                both("    ⚠️  WARNING: High temperature - may affect performance")
            elif temp > 60:
                both("    ⚠️  CAUTION: Elevated temperature - monitor")
            else:
                both("    ✓ Temperature is within normal range")
        except Exception:
            pass
    
    if module_vcc != "N/A":
        try:
            voltage = float(module_vcc)
            both(f"  Module Voltage              : {voltage} mV")
            # Typical range for optical modules is 2.97V - 3.63V
            if voltage < 2970 or voltage > 3630:
                both("    ⚠️  WARNING: Voltage outside typical range (2.97V - 3.63V)")
            else:
                both("    ✓ Voltage is within normal range")
        except Exception:
            pass
    
    # All Available CSV Fields (for completeness)
    both("\n" + "=" * 80)
    both("[All Available CSV Fields (Raw Data)]")
    both("=" * 80)
    both("  The following fields were found in the CSV (some may be empty):")
    both("")
    
    # Group fields by category for better readability
    field_categories = {
        "Port & Protocol": ["Port_Number", "MAC_Address", "Protocol", "Speed_[Gb/s]", "Active_FEC"],
        "Link Status": ["Link_Down", "Link_Down_GB_host", "Link_Down_GB_line", "Time_since_last_clear_[Min]"],
        "BER Metrics": ["Raw_BER", "Raw_BER_lane0", "Raw_BER_lane1", "Raw_BER_lane2", "Raw_BER_lane3", 
                       "Effective_BER"],
        "FEC Histogram": [f"hist{i}" for i in range(16)],
        "SNR": [f"snr_media_lane{i}" for i in range(4)] + [f"snr_host_lane{i}" for i in range(4)],
        "Cable/Module": ["Cable_PN", "Cable_SN", "cable_technology", "cable_type", "cable_vendor", 
                        "cable_length", "vendor_name", "Module_Temperature", "Module_Voltage"],
        "Link Events": ["successful_recovery_events", "total_successful_recovery_events",
                       "unintentional_link_down_events", "intentional_link_down_events",
                       "local_reason_opcode", "down_blame"]
    }
    
    displayed_fields = set()
    for category, fields in field_categories.items():
        category_fields = []
        for field in fields:
            if field in row:
                displayed_fields.add(field)
                value = safe_get(row, field, "")
                if value != "N/A" and value.strip():
                    category_fields.append(f"    {field:40s} : {value}")
        
        if category_fields:
            both(f"  [{category}]")
            for line in category_fields:
                both(line)
            both("")
    
    # Show any remaining fields not in categories
    remaining_fields = [k for k in row.keys() if k not in displayed_fields and safe_get(row, k, "").strip() and safe_get(row, k, "") != "N/A"]
    if remaining_fields:
        both("  [Other Fields]")
        for field in sorted(remaining_fields):
            value = safe_get(row, field, "")
            if value and value != "N/A":
                both(f"    {field:40s} : {value}")
        both("")
    
    both("")  # spacing


def find_csv_files_in_directory(directory: str, max_results: int = 10) -> List[str]:
    """Find CSV files in a directory."""
    csv_files = []
    try:
        if os.path.isdir(directory):
            for item in os.listdir(directory):
                item_path = os.path.join(directory, item)
                if os.path.isfile(item_path) and item.lower().endswith('.csv'):
                    csv_files.append(item_path)
                if len(csv_files) >= max_results:
                    break
    except Exception as e:
        # Silently fail - directory might not be accessible
        pass
    return sorted(csv_files)


def create_template_csv(path: str) -> bool:
    """
    Create a template amBER CSV file with all expected columns.
    Returns True if successful, False otherwise.
    """
    # Standard amBER CSV columns (based on typical amBER output)
    template_columns = [
        "amBer_Version", "TimeStamp", "Iteration/Sweep", "Test_Description",
        "collection_tool", "collection_tool_version", "MAC_Address", "Port_Number",
        "Label_cage", "Phy_Manager_State", "Protocol", "Speed_[Gb/s]",
        "Ethernet_Protocol_Active", "Link_Down", "successful_recovery_events",
        "Raw_BER_lane0", "Raw_BER_lane1", "Raw_BER_lane2", "Raw_BER_lane3",
        "Raw_BER_lane4", "Raw_BER_lane5", "Raw_BER_lane6", "Raw_BER_lane7",
        "Link_Down_GB_host", "Link_Down_GB_line", "Active_FEC",
        "USR-T_Link_Down", "USR-M_Link_Down", "Time_since_last_clear_[Min]",
        "Conf_Level_Raw_BER", "Raw_BER", "Effective_BER", "FC_Zero_Hist",
        "Number_of_histogram_bins", "bin0_high_value", "bin0_low_value",
        "bin1_high_value", "bin1_low_value", "bin2_high_value", "bin2_low_value",
        "bin3_high_value", "bin3_low_value", "bin4_high_value", "bin4_low_value",
        "bin5_high_value", "bin5_low_value", "bin6_high_value", "bin6_low_value",
        "bin7_high_value", "bin7_low_value", "bin8_high_value", "bin8_low_value",
        "bin9_high_value", "bin9_low_value", "bin10_high_value", "bin10_low_value",
        "bin11_high_value", "bin11_low_value", "bin12_high_value", "bin12_low_value",
        "bin13_high_value", "bin13_low_value", "bin14_high_value", "bin14_low_value",
        "bin15_high_value", "bin15_low_value", "hist0", "hist1", "hist2", "hist3",
        "hist4", "hist5", "hist6", "hist7", "hist8", "hist9", "hist10", "hist11",
        "hist12", "hist13", "hist14", "hist15", "Raw_Errors_lane0", "Raw_Errors_lane1",
        "Raw_Errors_lane2", "Raw_Errors_lane3", "Raw_Errors_lane4", "Raw_Errors_lane5",
        "Raw_Errors_lane6", "Raw_Errors_lane7", "Effective_Errors", "Symbol_Errors",
        "module_oper_status", "module_error_type", "ethernet_compliance_code",
        "ext_ethernet_compliance_code", "Memory_map_rev", "Vendor_OUI", "Cable_PN",
        "Cable_SN", "cable_technology", "linear_direct_drive", "cable_breakout",
        "cable_type", "cable_vendor", "cable_length", "smf_length", "cable_identifier",
        "cable_power_class", "max_power", "cable_rx_amp", "cable_rx_pre_emphasis",
        "cable_rx_post_emphasis", "cable_tx_equalization", "cable_attenuation_53g",
        "cable_attenuation_25g", "cable_attenuation_12g", "cable_attenuation_7g",
        "cable_attenuation_5g", "tx_input_freq_sync", "rx_cdr_cap", "tx_cdr_cap",
        "rx_cdr_state", "tx_cdr_state", "vendor_name", "vendor_rev", "module_fw_version",
        "rx_power_lane_0", "rx_power_lane_1", "rx_power_lane_2", "rx_power_lane_3",
        "rx_power_lane_4", "rx_power_lane_5", "rx_power_lane_6", "rx_power_lane_7",
        "tx_power_lane_0", "tx_power_lane_1", "tx_power_lane_2", "tx_power_lane_3",
        "tx_power_lane_4", "tx_power_lane_5", "tx_power_lane_6", "tx_power_lane_7",
        "tx_bias_lane_0", "tx_bias_lane_1", "tx_bias_lane_2", "tx_bias_lane_3",
        "tx_bias_lane_4", "tx_bias_lane_5", "tx_bias_lane_6", "tx_bias_lane_7",
        "temperature_high_th", "temperature_low_th", "voltage_high_th", "voltage_low_th",
        "rx_power_high_th", "rx_power_low_th", "tx_power_high_th", "tx_power_low_th",
        "tx_bias_high_th", "tx_bias_low_th", "wavelength", "wavelength_tolerance",
        "Module_st", "Dp_st_lane0", "Dp_st_lane1", "Dp_st_lane2", "Dp_st_lane3",
        "Dp_st_lane4", "Dp_st_lane5", "Dp_st_lane6", "Dp_st_lane7", "rx_output_valid",
        "Nominal_Bit_Rate", "Rx_Power_Type", "Date_Code", "Module_Temperature",
        "Module_Voltage", "Active_set_host_compliance_code", "Active_set_media_compliance_code",
        "error_code_response", "Temp_flags", "Vcc_flags", "Mod_fw_fault", "Dp_fw_fault",
        "tx_fault", "tx_los", "tx_cdr_lol", "tx_ad_eq_fault", "tx_power_hi_al",
        "tx_power_lo_al", "tx_power_hi_war", "tx_power_lo_war", "tx_bias_hi_al",
        "tx_bias_lo_al", "tx_bias_hi_war", "tx_bias_lo_war", "rx_los", "rx_cdr_lol",
        "rx_power_hi_al", "rx_power_lo_al", "rx_power_hi_war", "rx_power_lo_war",
        "laser_status", "laser_restriction", "els_oper_state", "els_laser_fault_state",
        "MCM_system", "Tile_Num", "slot_index", "Module_Lanes_Used", "PLL_Index",
        "Retimer_valid", "Retimer_dp_num", "Retimer_die_num", "Device_Description",
        "Device_Part_Number", "Device_FW_Version", "Device_ID", "SerDes_Technology_(16nm/7nm_5nm)",
        "System_Voltage", "System_Current", "Voltage/Current_sensor_name", "Chip_Temp",
        "Temp_sensor_name", "Device_SN", "UPHY_version", "BKV_version",
        "Lane0_pre_3_tap", "Lane1_pre_3_tap", "Lane2_pre_3_tap", "Lane3_pre_3_tap",
        "Lane4_pre_3_tap", "Lane5_pre_3_tap", "Lane6_pre_3_tap", "Lane7_pre_3_tap",
        "Lane0_pre_2_tap", "Lane1_pre_2_tap", "Lane2_pre_2_tap", "Lane3_pre_2_tap",
        "Lane4_pre_2_tap", "Lane5_pre_2_tap", "Lane6_pre_2_tap", "Lane7_pre_2_tap",
        "Lane0_pre_1_tap", "Lane1_pre_1_tap", "Lane2_pre_1_tap", "Lane3_pre_1_tap",
        "Lane4_pre_1_tap", "Lane5_pre_1_tap", "Lane6_pre_1_tap", "Lane7_pre_1_tap",
        "Lane0_main_tap", "Lane1_main_tap", "Lane2_main_tap", "Lane3_main_tap",
        "Lane4_main_tap", "Lane5_main_tap", "Lane6_main_tap", "Lane7_main_tap",
        "Lane0_post_1_tap", "Lane1_post_1_tap", "Lane2_post_1_tap", "Lane3_post_1_tap",
        "Lane4_post_1_tap", "Lane5_post_1_tap", "Lane6_post_1_tap", "Lane7_post_1_tap",
        "Advanced_Status_Opcode", "Status_Message", "eth_an_fsm_state", "ib_phy_fsm_state",
        "phy_manager_link_enabled", "core_to_phy_link_enabled", "cable_proto_cap",
        "loopback_mode", "fec_mode_request", "profile_fec_in_use", "up_reason_pwr",
        "up_reason_drv", "up_reason_mng", "time_to_link_up_msec", "fast_link_up_status",
        "time_to_link_up_phy_up_to_active", "time_to_link_up_sd_to_phy_up",
        "time_to_link_up_disable_to_sd", "time_to_link_up_disable_to_pd",
        "time_of_module_conf_done_up", "time_of_module_conf_done_down",
        "time_logical_init_to_active", "down_blame", "local_reason_opcode",
        "remote_reason_opcode", "e2e_reason_opcode", "time_to_link_down_to_disable",
        "time_to_link_down_to_rx_loss", "num_of_raw_ber_alarms", "num_of_symbol_ber_alarms",
        "num_of_eff_ber_alarms", "last_raw_ber", "last_eff_ber", "last_symbol_ber",
        "max_raw_ber", "max_effective_ber", "max_symbol_ber", "min_raw_ber",
        "min_effective_ber", "min_symbol_ber", "snr_media_lane0", "snr_media_lane1",
        "snr_media_lane2", "snr_media_lane3", "snr_media_lane4", "snr_media_lane5",
        "snr_media_lane6", "snr_media_lane7", "snr_host_lane0", "snr_host_lane1",
        "snr_host_lane2", "snr_host_lane3", "snr_host_lane4", "snr_host_lane5",
        "snr_host_lane6", "snr_host_lane7", "voltage_pemi", "module_st_pemi",
        "rx_power_lane0_pemi", "rx_power_lane1_pemi", "rx_power_lane2_pemi",
        "rx_power_lane3_pemi", "rx_power_lane4_pemi", "rx_power_lane5_pemi",
        "rx_power_lane6_pemi", "rx_power_lane7_pemi", "tx_power_lane0_pemi",
        "tx_power_lane1_pemi", "tx_power_lane2_pemi", "tx_power_lane3_pemi",
        "tx_power_lane4_pemi", "tx_power_lane5_pemi", "tx_power_lane6_pemi",
        "tx_power_lane7_pemi", "tx_bias_lane0_pemi", "tx_bias_lane1_pemi",
        "tx_bias_lane2_pemi", "tx_bias_lane3_pemi", "tx_bias_lane4_pemi",
        "tx_bias_lane5_pemi", "tx_bias_lane6_pemi", "tx_bias_lane7_pemi",
        "dp_st_lane0_pemi", "dp_st_lane1_pemi", "dp_st_lane2_pemi", "dp_st_lane3_pemi",
        "dp_st_lane4_pemi", "dp_st_lane5_pemi", "dp_st_lane6_pemi", "dp_st_lane7_pemi",
        "oe_ts1_temperature", "els_ts1_temperature", "laser_frequency_error_lane0",
        "laser_frequency_error_lane1", "laser_frequency_error_lane2",
        "laser_frequency_error_lane3", "laser_frequency_error_lane4",
        "laser_frequency_error_lane5", "laser_frequency_error_lane6",
        "laser_frequency_error_lane7", "cooled_laser_temperature_lane0",
        "cooled_laser_temperature_lane1", "cooled_laser_temperature_lane2",
        "cooled_laser_temperature_lane3", "cooled_laser_temperature_lane4",
        "cooled_laser_temperature_lane5", "cooled_laser_temperature_lane6",
        "cooled_laser_temperature_lane7", "icc_monitor", "els_power_consumption",
        "pre_fec_ber_min_media", "pre_fec_ber_min_host", "pre_fec_ber_max_media",
        "pre_fec_ber_max_host", "pre_fec_ber_avg_media", "pre_fec_ber_avg_host",
        "pre_fec_ber_val_media", "pre_fec_ber_val_host", "pre_fec_ber_cap",
        "temp_threshold_1", "temp_threshold_2", "temp_threshold_3", "temp_threshold_4",
        "temp_thr_1_counter", "temp_thr_2_counter", "temp_thr_3_counter",
        "temp_thr_4_counter", "abs_max_temp_change", "operational_recovery",
        "total_successful_recovery_events", "successful_recovery_events_cnt",
        "unintentional_link_down_events", "intentional_link_down_events",
        "time_in_last_host_logical_recovery", "time_in_last_host_serdes_feq_recovery",
        "time_in_last_module_tx_disable_recovery",
        "time_in_last_module_datapath_full_toggle_recovery",
        "total_time_in_host_logical_recovery", "total_time_in_host_serdes_feq_recovery",
        "total_time_in_module_datapath_full_toggle_recovery",
        "total_host_logical_recovery_count", "total_host_serdes_feq_recovery_count",
        "total_module_tx_disable_recovery_count",
        "total_module_datapath_full_toggle_recovery_count",
        "total_host_logical_succesful_recovery_count",
        "total_host_serdes_feq_succesful_recovery_count",
        "total_module_tx_disable_succesful_recovery_count",
        "total_module_datapath_full_toggle_succesful_recovery_count",
        "time_since_last_recovery", "last_host_logical_recovery_attempts_count",
        "last_host_serdes_feq_attempts_count", "time_between_last_2_recoveries",
        "last_rs_fec_uncorrectable_during_recovery"
    ]
    
    try:
        # Ensure directory exists
        file_dir = os.path.dirname(os.path.abspath(path))
        if file_dir and not os.path.exists(file_dir):
            os.makedirs(file_dir, exist_ok=True)
        
        # Write header row
        with open(path, 'w', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(template_columns)
        
        return True
    except Exception as e:
        return False


def process_file(path: str, logf: TextIO, if_map: Dict[str, Dict[str, str]]) -> None:
    # Normalize the path - handle relative paths
    original_path = path
    if not os.path.isabs(path):
        # It's a relative path, try current directory first
        abs_path = os.path.abspath(path)
        if os.path.exists(abs_path):
            path = abs_path
    else:
        abs_path = path
    
    # Check if file exists and provide better error messages
    if not os.path.exists(path):
        log_msg(f"[ERROR] {original_path}: file does not exist.", logf)
        log_msg(f"[ERROR] Current directory: {os.getcwd()}", logf)
        if original_path != abs_path:
            log_msg(f"[ERROR] Tried absolute path: {abs_path}", logf)
        
        # Try to find CSV files in the same directory
        file_dir = os.path.dirname(abs_path) if os.path.isabs(original_path) else os.getcwd()
        csv_files = find_csv_files_in_directory(file_dir)
        found_any = False
        
        if csv_files:
            found_any = True
            log_msg(f"[INFO] Found {len(csv_files)} CSV file(s) in {file_dir}:", logf)
            for csv_file in csv_files[:5]:  # Show first 5
                # Show relative path if in current directory
                if file_dir == os.getcwd():
                    rel_path = os.path.relpath(csv_file, os.getcwd())
                    log_msg(f"[INFO]   - {rel_path} (or {csv_file})", logf)
                else:
                    log_msg(f"[INFO]   - {csv_file}", logf)
            if len(csv_files) > 5:
                log_msg(f"[INFO]   ... and {len(csv_files) - 5} more", logf)
        else:
            # Search in common locations including current directory
            search_dirs = [os.getcwd(), '/root', '/tmp', '/var/log']
            for search_dir in search_dirs:
                if os.path.exists(search_dir):
                    csv_files = find_csv_files_in_directory(search_dir, max_results=3)
                    if csv_files:
                        if not found_any:
                            log_msg(f"[INFO] Searching for CSV files in common locations:", logf)
                            found_any = True
                        log_msg(f"[INFO]   {search_dir}:", logf)
                        for csv_file in csv_files:
                            # Show relative path if in current directory
                            if search_dir == os.getcwd():
                                rel_path = os.path.relpath(csv_file, os.getcwd())
                                log_msg(f"[INFO]     - {rel_path} (or {csv_file})", logf)
                            else:
                                log_msg(f"[INFO]     - {csv_file}", logf)
            
            if not found_any:
                log_msg(f"[INFO] No CSV files found in current directory or common locations.", logf)
                log_msg(f"[INFO] Searched in: {', '.join(search_dirs)}", logf)
        
        log_msg(f"[ERROR] Please check the file path and try again.", logf)
        if found_any:
            log_msg(f"[TIP] Try using the full path or one of the files listed above.", logf)
            log_msg(f"[INFO] Alternatively, creating a template CSV file at {abs_path}...", logf)
        else:
            log_msg(f"[TIP] Creating a template CSV file since none were found.", logf)
        
        # Always create the template file for the requested path
        log_msg(f"[INFO] Creating template CSV file with expected column headers at {abs_path}...", logf)
        if create_template_csv(abs_path):
            log_msg(f"[SUCCESS] Template CSV file created: {abs_path}", logf)
            log_msg(f"[INFO] This is a template file with column headers only.", logf)
            log_msg(f"[INFO] You can now populate it with actual amBER data or use it as a reference.", logf)
            log_msg(f"[INFO] Attempting to process the newly created file...", logf)
            # Close current log file and reopen to append
            logf.flush()
            # Now try to process the newly created file
            try:
                rows = read_csv_safely(abs_path)
                if not rows:
                    log_msg(f"[WARN] {abs_path}: CSV has no data rows (only headers).", logf)
                    log_msg(f"[INFO] This is expected for a template file. Add data rows to process.", logf)
                else:
                    log_msg(f"[INFO] Found {len(rows)} data row(s) in the file.", logf)
                    for i, row in enumerate(rows):
                        summarize_row(row, abs_path, i, logf, if_map)
            except Exception as e:
                log_msg(f"[ERROR] Failed to process newly created file: {e}", logf)
        else:
            log_msg(f"[ERROR] Failed to create template CSV file. Check permissions.", logf)
        return
    
    if os.path.isdir(path):
        log_msg(f"[ERROR] {path}: is a directory, not a file.", logf)
        return
    
    if not os.path.isfile(path):
        # Check if it's a symlink
        if os.path.islink(path):
            real_path = os.path.realpath(path)
            log_msg(f"[INFO] {path}: is a symlink pointing to {real_path}", logf)
            if os.path.isfile(real_path):
                log_msg(f"[INFO] Following symlink to process: {real_path}", logf)
                path = real_path
            else:
                log_msg(f"[ERROR] Symlink target {real_path} is not a regular file.", logf)
                return
        else:
            log_msg(f"[ERROR] {path}: not a regular file, skipping.", logf)
            return

    try:
        rows = read_csv_safely(path)
    except Exception as e:
        log_msg(f"[ERROR] Failed to read {path}: {e}", logf)
        import traceback
        log_msg(f"[ERROR] Traceback: {traceback.format_exc()}", logf)
        return

    if not rows:
        log_msg(f"[WARN] {path}: CSV has no valid data rows (only headers or empty).", logf)
        
        # Try to read the header to provide more information
        try:
            with open(path, "rb") as f:
                first_line = f.readline()
                if first_line:
                    cleaned = clean_line(first_line)
                    if cleaned.strip():
                        # Parse header
                        header_reader = csv.reader([cleaned])
                        try:
                            headers = next(header_reader)
                            if headers:
                                log_msg(f"[INFO] CSV file structure:", logf)
                                log_msg(f"[INFO]   Total columns: {len(headers)}", logf)
                                log_msg(f"[INFO]   File size: {os.path.getsize(path)} bytes", logf)
                                log_msg(f"[INFO]   This appears to be a template file with column headers only.", logf)
                                log_msg(f"[INFO]   Key columns found:", logf)
                                
                                # Show important columns
                                important_cols = [
                                    "MAC_Address", "Port_Number", "Protocol", "Speed_[Gb/s]",
                                    "Raw_BER", "Effective_BER", "Link_Down", "Active_FEC",
                                    "Cable_PN", "Cable_SN", "Module_Temperature", "Module_Voltage"
                                ]
                                found_important = [col for col in important_cols if col in headers]
                                if found_important:
                                    for col in found_important[:10]:  # Show first 10
                                        log_msg(f"[INFO]     - {col}", logf)
                                    if len(found_important) > 10:
                                        log_msg(f"[INFO]     ... and {len(found_important) - 10} more important columns", logf)
                                
                                log_msg(f"[INFO]   To process this file, add data rows below the header row.", logf)
                        except Exception:
                            pass
        except Exception:
            pass
        
        return

    for i, row in enumerate(rows):
        summarize_row(row, path, i, logf, if_map)


# -------------------- Main -------------------- #

def expand_file_patterns(patterns: List[str]) -> List[str]:
    """Expand wildcard patterns to actual file paths."""
    import glob
    expanded = []
    for pattern in patterns:
        # Check if pattern contains wildcards
        if '*' in pattern or '?' in pattern or '[' in pattern:
            # Expand glob patterns
            matches = glob.glob(pattern)
            if matches:
                expanded.extend(sorted(matches))
            else:
                # No matches found for wildcard - don't create template with wildcard name
                continue
        else:
            # No wildcards, treat as literal file path
            expanded.append(pattern)
    return expanded


def check_mst_installed() -> bool:
    """Check if MST (mst command) is installed."""
    try:
        result = subprocess.run(["which", "mst"], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False


def install_mst() -> bool:
    """Install MST (Mellanox Firmware Tools) from Mellanox website."""
    import tempfile
    import shutil
    
    mst_url = "https://www.mellanox.com/downloads/MFT/mft-4.33.0-169-x86_64-deb.tgz"
    mst_tgz = "mft-4.33.0-169-x86_64-deb.tgz"
    mst_dir = "mft-4.33.0-169-x86_64-deb"
    
    log_msg("[INFO] Installing MST (Mellanox Firmware Tools)...", None)
    log_msg(f"[INFO] Downloading from: {mst_url}", None)
    
    original_dir = os.getcwd()
    temp_dir = None
    
    try:
        # Create temporary directory
        temp_dir = tempfile.mkdtemp(prefix="mst_install_")
        os.chdir(temp_dir)
        
        # Download
        log_msg("[INFO] Downloading MST package (this may take a few minutes)...", None)
        result = subprocess.run(["wget", mst_url], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            log_msg(f"[ERROR] Failed to download MST.", None)
            if result.stderr:
                log_msg(f"[ERROR] {result.stderr}", None)
            return False
        
        if not os.path.exists(mst_tgz):
            log_msg("[ERROR] Downloaded file not found.", None)
            return False
        
        # Extract
        log_msg("[INFO] Extracting MST package...", None)
        result = subprocess.run(["tar", "-xvf", mst_tgz], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log_msg(f"[ERROR] Failed to extract MST: {result.stderr}", None)
            return False
        
        if not os.path.exists(mst_dir):
            log_msg("[ERROR] Extracted directory not found.", None)
            return False
        
        # Install
        log_msg("[INFO] Installing MST packages...", None)
        os.chdir(mst_dir)
        
        # Check for install script first
        if os.path.exists("install.sh"):
            log_msg("[INFO] Running install.sh...", None)
            result = subprocess.run(["bash", "install.sh"], capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                log_msg("[SUCCESS] MST installed successfully.", None)
                return True
            else:
                # Try with sudo
                log_msg("[INFO] Trying with sudo...", None)
                result = subprocess.run(["sudo", "bash", "install.sh"], capture_output=True, text=True, timeout=300)
        
        # If no install script or it failed, try installing .deb files directly
        if result.returncode != 0 or not os.path.exists("install.sh"):
            deb_files = [f for f in os.listdir(".") if f.endswith(".deb")]
            if deb_files:
                log_msg(f"[INFO] Found {len(deb_files)} .deb file(s), installing...", None)
                for deb_file in sorted(deb_files):
                    log_msg(f"[INFO] Installing {deb_file}...", None)
                    result = subprocess.run(["sudo", "dpkg", "-i", deb_file], capture_output=True, text=True, timeout=300)
                    if result.returncode != 0:
                        # Try to fix dependencies
                        log_msg("[INFO] Fixing dependencies...", None)
                        subprocess.run(["sudo", "apt-get", "install", "-f", "-y"], capture_output=True, text=True, timeout=300)
                        result = subprocess.run(["sudo", "dpkg", "-i", deb_file], capture_output=True, text=True, timeout=300)
            else:
                log_msg("[ERROR] No install script or .deb files found", None)
                return False
        
        if result.returncode == 0:
            log_msg("[SUCCESS] MST installed successfully.", None)
            return True
        else:
            log_msg(f"[ERROR] Installation failed.", None)
            if result.stderr:
                log_msg(f"[ERROR] {result.stderr}", None)
            return False
            
    except subprocess.TimeoutExpired:
        log_msg("[ERROR] Installation timed out.", None)
        return False
    except Exception as e:
        log_msg(f"[ERROR] Failed to install MST: {e}", None)
        import traceback
        log_msg(f"[ERROR] Traceback: {traceback.format_exc()}", None)
        return False
    finally:
        # Cleanup
        try:
            os.chdir(original_dir)
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def check_mst_running() -> bool:
    """Check if MST (Mellanox Software Tools) is running."""
    mst_dir = "/dev/mst"
    if not os.path.exists(mst_dir):
        return False
    
    # Check if there are any MST devices
    try:
        items = os.listdir(mst_dir)
        for item in items:
            if item.startswith("mt") and "pciconf" in item:
                device_path = os.path.join(mst_dir, item)
                if os.path.exists(device_path):
                    return True
    except Exception:
        pass
    
    return False


def start_mst() -> bool:
    """Start MST (Mellanox Software Tools) driver."""
    try:
        log_msg("[INFO] Starting MST (Mellanox Software Tools)...", None)
        result = subprocess.run(["mst", "start"], capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            log_msg("[SUCCESS] MST started successfully.", None)
            # Wait a moment for devices to appear
            import time
            time.sleep(2)
            return True
        else:
            log_msg(f"[ERROR] Failed to start MST: {result.stderr}", None)
            return False
    except FileNotFoundError:
        log_msg("[ERROR] 'mst' command not found. Please install Mellanox Firmware Tools (MFT).", None)
        return False
    except subprocess.TimeoutExpired:
        log_msg("[ERROR] MST start command timed out.", None)
        return False
    except Exception as e:
        log_msg(f"[ERROR] Failed to start MST: {e}", None)
        return False


def find_all_mst_devices() -> List[str]:
    """Find all MST devices in /dev/mst/. Returns empty list if MST not running."""
    devices = []
    mst_dir = "/dev/mst"
    if os.path.exists(mst_dir):
        for item in os.listdir(mst_dir):
            if item.startswith("mt") and "pciconf" in item:
                device_path = os.path.join(mst_dir, item)
                if os.path.exists(device_path):
                    devices.append(device_path)
    return sorted(devices)


def get_link_name_from_csv(csv_file: str) -> str:
    """Extract link/port name from CSV file to use in filename."""
    try:
        rows = read_csv_safely(csv_file)
        if rows:
            # Get port number from first row
            port = safe_get(rows[0], "Port_Number", "")
            # Clean port name for filename (remove special chars)
            if port:
                port_clean = port.replace("(", "_").replace(")", "").replace("/", "_").replace(" ", "_")
                return port_clean
    except Exception:
        pass
    return ""


def get_interface_name_from_csv(csv_file: str, if_map: Dict[str, Dict[str, str]]) -> str:
    """Extract interface name from CSV file using MAC address mapping."""
    try:
        rows = read_csv_safely(csv_file)
        if rows:
            # Get MAC address from first row
            mac_hex = safe_get(rows[0], "MAC_Address", "")
            mac_addr = mac_from_amber_hex(mac_hex)
            if mac_addr and mac_addr.lower() in if_map:
                ifname = if_map[mac_addr.lower()].get("ifname", "")
                if ifname:
                    return ifname
    except Exception:
        pass
    return ""


def capture_kernel_messages(output_file: str) -> str:
    """Capture kernel messages (dmesg) with timestamps to a file."""
    kernel_log_file = output_file.replace(".csv", "_kernel.log")
    
    try:
        log_msg(f"[INFO] Capturing kernel messages to: {kernel_log_file}", None)
        
        # Get kernel messages with timestamps (relative to boot)
        result = subprocess.run(["dmesg", "-T"], capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            with open(kernel_log_file, "w", encoding="utf-8") as f:
                f.write("=" * 80 + "\n")
                f.write(f"Kernel Messages (dmesg) Capture\n")
                f.write(f"Captured: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}\n")
                f.write("=" * 80 + "\n\n")
                f.write(result.stdout)
            
            # Count link-related messages
            link_messages = [line for line in result.stdout.splitlines() if "link" in line.lower() or "carrier" in line.lower()]
            
            log_msg(f"[SUCCESS] Kernel messages captured: {kernel_log_file}", None)
            log_msg(f"[INFO] Found {len(link_messages)} link-related kernel messages.", None)
            
            return kernel_log_file
        else:
            log_msg(f"[ERROR] Failed to capture kernel messages: {result.stderr}", None)
            return ""
    except FileNotFoundError:
        log_msg("[ERROR] dmesg command not found.", None)
        return ""
    except subprocess.TimeoutExpired:
        log_msg("[ERROR] Kernel message capture timed out.", None)
        return ""
    except Exception as e:
        log_msg(f"[ERROR] Failed to capture kernel messages: {e}", None)
        return ""


def collect_amber_data(device: str, port: Optional[int] = None, output_file: str = "amber_data.csv", if_map: Optional[Dict[str, Dict[str, str]]] = None) -> str:
    """Collect amBER data using mlxlink and save to CSV file. Returns final filename with link and interface name."""
    try:
        # Create temporary filename first
        temp_file = output_file.replace(".csv", "_temp.csv")
        
        cmd = ["mlxlink", "-d", device]
        if port is not None:
            cmd.extend(["-p", str(port)])
        
        # Use --amber_collect with temporary output file
        cmd.extend(["--amber_collect", temp_file])
        
        log_msg(f"[INFO] Collecting amBER data from {device}" + (f" port {port}" if port else ""), None)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        if result.returncode == 0:
            if os.path.exists(temp_file):
                # Read CSV to get link name
                link_name = get_link_name_from_csv(temp_file)
                
                # Get interface name from MAC mapping if available
                interface_name = ""
                if if_map:
                    interface_name = get_interface_name_from_csv(temp_file, if_map)
                
                # Create final filename with link name and interface name
                base_name = output_file.replace(".csv", "")
                parts = [base_name]
                
                if link_name:
                    parts.append(link_name)
                
                if interface_name:
                    parts.append(interface_name)
                
                final_file = "_".join(parts) + ".csv"
                
                # Rename temp file to final filename
                if final_file != temp_file:
                    os.rename(temp_file, final_file)
                else:
                    # If same name, just rename temp
                    if temp_file != output_file:
                        os.rename(temp_file, output_file)
                        final_file = output_file
                
                log_msg(f"[SUCCESS] amBER data collected and saved to: {final_file}", None)
                return final_file
            else:
                log_msg(f"[ERROR] mlxlink completed but file not created: {temp_file}", None)
                return ""
        else:
            log_msg(f"[ERROR] mlxlink failed. Try running manually:", None)
            log_msg(f"  mlxlink -d {device}" + (f" -p {port}" if port else "") + f" --amber_collect {output_file}", None)
            if result.stderr:
                log_msg(f"  Error: {result.stderr}", None)
            return ""
    except FileNotFoundError:
        log_msg("[ERROR] mlxlink not found. Please install Mellanox Firmware Tools (MFT).", None)
        return ""
    except subprocess.TimeoutExpired:
        log_msg("[ERROR] mlxlink command timed out.", None)
        return ""
    except Exception as e:
        log_msg(f"[ERROR] Failed to collect amBER data: {e}", None)
        return ""


def main():
    parser = argparse.ArgumentParser(
        description="Summarize NVIDIA amBER CSV output into human-readable link health reports.",
        epilog="Examples:\n"
               "  python3 amber_summarize.py file.csv\n"
               "  python3 amber_summarize.py *.csv\n"
               "  python3 amber_summarize.py --collect /dev/mst/mt41692_pciconf0\n"
               "  python3 amber_summarize.py --collect /dev/mst/mt41692_pciconf0 -p 1\n",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="One or more amBER CSV files (supports wildcards like *.csv)"
    )
    parser.add_argument(
        "--collect",
        metavar="DEVICE",
        nargs="?",
        const="all",
        help="Collect amBER data using mlxlink. Use 'all' or specify device (e.g., /dev/mst/mt41692_pciconf0)"
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        help="Port number when using --collect option"
    )
    parser.add_argument(
        "-o", "--output",
        default="amber_data.csv",
        help="Output CSV filename prefix when using --collect option (default: amber_data.csv)"
    )
    parser.add_argument(
        "--install-mst",
        action="store_true",
        help="Install MST (Mellanox Firmware Tools) if not already installed"
    )
    args = parser.parse_args()
    
    # Check if MST installation is requested
    if args.install_mst:
        if check_mst_installed():
            log_msg("[INFO] MST is already installed.", None)
        else:
            if not install_mst():
                log_msg("[ERROR] Failed to install MST. Please install manually.", None)
                return
        # After installation, also start MST
        if not check_mst_running():
            log_msg("[INFO] Starting MST after installation...", None)
            start_mst()
    
    # Build host MAC -> interface map once (before collecting data so we can use it in filenames)
    if_map = get_local_if_map()
    if if_map:
        log_msg(f"[INFO] Found {len(if_map)} local interface(s) for MAC mapping.", None)
    
    # If --collect is used, collect data first
    if args.collect:
        collected_files = []
        
        if args.collect.lower() == "all" or args.collect == "all":
            # Check if MST is running
            if not check_mst_running():
                log_msg("[WARN] MST (Mellanox Software Tools) is not running.", None)
                log_msg("[INFO] Attempting to start MST...", None)
                if not start_mst():
                    log_msg("[ERROR] Could not start MST. Please run 'mst start' manually.", None)
                    return
            
            # Collect from all MST devices
            devices = find_all_mst_devices()
            if not devices:
                log_msg("[ERROR] No MST devices found in /dev/mst/", None)
                log_msg("[INFO] Try running 'mst start' to initialize MST devices.", None)
                return
            
            log_msg(f"[INFO] Found {len(devices)} MST device(s), collecting amBER data from all...", None)
            
            for device in devices:
                device_name = os.path.basename(device).replace("pciconf", "").replace("_", "")
                output_file = f"{args.output.replace('.csv', '')}_{device_name}.csv"
                
                final_file = collect_amber_data(device, args.port, output_file, if_map)
                if final_file:
                    collected_files.append(final_file)
                    # Capture kernel messages for each device
                    kernel_log = capture_kernel_messages(final_file)
                else:
                    log_msg(f"[WARN] Failed to collect from {device}, continuing...", None)
            
            if not collected_files:
                log_msg("[ERROR] Failed to collect data from any device.", None)
                return
            
            # Capture overall kernel messages after collection
            if collected_files:
                overall_kernel_log = capture_kernel_messages(collected_files[0].replace("_mt", "_all_devices"))
                if overall_kernel_log:
                    log_msg(f"[INFO] Overall kernel messages captured: {overall_kernel_log}", None)
        else:
            # Collect from specified device
            # Check if MST is running
            if not check_mst_running():
                log_msg("[WARN] MST (Mellanox Software Tools) is not running.", None)
                log_msg("[INFO] Attempting to start MST...", None)
                if not start_mst():
                    log_msg("[ERROR] Could not start MST. Please run 'mst start' manually.", None)
                    return
            
            # Verify the specified device exists
            if not os.path.exists(args.collect):
                log_msg(f"[ERROR] Device not found: {args.collect}", None)
                log_msg("[INFO] Available devices:", None)
                devices = find_all_mst_devices()
                if devices:
                    for dev in devices:
                        log_msg(f"  - {dev}", None)
                else:
                    log_msg("  (none - MST may not be running)", None)
                return
            
            final_file = collect_amber_data(args.collect, args.port, args.output, if_map)
            if not final_file:
                return
            collected_files = [final_file]
            # Capture kernel messages for this device
            kernel_log = capture_kernel_messages(final_file)
        
        # Add collected files to the files list
        if not args.files:
            args.files = collected_files
        else:
            args.files = collected_files + args.files
    
    if not args.files:
        parser.error("Either provide CSV files or use --collect option to gather data.")

    # Expand wildcard patterns
    file_paths = expand_file_patterns(args.files)
    
    if not file_paths:
        log_msg("[ERROR] No files found matching the pattern.", None)
        return

    # if_map already built above if --collect was used, otherwise build it now
    if not args.collect:
        if_map = get_local_if_map()
        if if_map:
            log_msg(f"[INFO] Found {len(if_map)} local interface(s) for MAC mapping.", None)

    # Summary tracking
    processed_files = []
    created_templates = []
    errors = []
    kernel_logs = []

    log_msg(f"[INFO] Processing {len(file_paths)} file(s)...", None)
    log_msg("", None)
    
    # Capture kernel messages once at the start (for all files)
    if file_paths:
        # Use first file's name as base for kernel log
        first_file = file_paths[0]
        kernel_log = capture_kernel_messages(first_file)
        if kernel_log:
            kernel_logs.append(kernel_log)

    for path in file_paths:
        log_path = f"{path}.log"
        # Ensure log directory exists
        log_dir = os.path.dirname(log_path)
        if log_dir and not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir, exist_ok=True)
            except Exception as e:
                log_msg(f"[WARN] Could not create log directory {log_dir}: {e}", None)
        
        try:
            with open(log_path, "w", encoding="utf-8") as logf:
                log_msg(f"[INFO] Processing: {path}", logf)
                log_msg(f"[INFO] Log file: {log_path}", logf)
                
                # Check if file exists before processing
                file_existed = os.path.exists(path)
                process_file(path, logf, if_map)
                
                # Track what happened
                if not file_existed and os.path.exists(path):
                    created_templates.append(path)
                elif file_existed:
                    # Check if it had data
                    try:
                        rows = read_csv_safely(path)
                        if rows:
                            processed_files.append((path, log_path, len(rows)))
                        else:
                            created_templates.append(path)
                    except:
                        processed_files.append((path, log_path, 0))
                
                log_msg("[INFO] Done.", logf)
        except Exception as e:
            error_msg = f"Failed to process {path}: {e}"
            log_msg(f"[ERROR] {error_msg}", None)
            errors.append((path, error_msg))

    # Print summary
    log_msg("", None)
    log_msg("=" * 80, None)
    log_msg("PROCESSING SUMMARY", None)
    log_msg("=" * 80, None)
    
    if processed_files:
        log_msg(f"[SUCCESS] Processed {len(processed_files)} file(s) with data:", None)
        for file_path, log_path, row_count in processed_files:
            log_msg(f"  ✓ {file_path} -> {log_path} ({row_count} row(s))", None)
        log_msg("", None)
        log_msg("  View detailed logs:", None)
        for file_path, log_path, _ in processed_files:
            log_msg(f"    cat {log_path}", None)
    
    if created_templates:
        log_msg(f"[INFO] Created {len(created_templates)} template file(s):", None)
        for file_path in created_templates:
            log_msg(f"  • {file_path} (template with headers only)", None)
        log_msg("", None)
        log_msg("  These are template files. Add data rows to generate detailed reports.", None)
    
    if errors:
        log_msg(f"[ERROR] {len(errors)} file(s) had errors:", None)
        for file_path, error_msg in errors:
            log_msg(f"  ✗ {file_path}: {error_msg}", None)
    
    if kernel_logs:
        log_msg("", None)
        log_msg(f"[INFO] Kernel messages captured in {len(kernel_logs)} file(s):", None)
        for kernel_log in kernel_logs:
            log_msg(f"  • {kernel_log}", None)
        log_msg("", None)
        log_msg("  View kernel messages:", None)
        for kernel_log in kernel_logs:
            log_msg(f"    cat {kernel_log}", None)
        log_msg("", None)
        log_msg("  Search for link events in kernel logs:", None)
        for kernel_log in kernel_logs:
            log_msg(f"    grep -i 'link' {kernel_log}", None)
    
    log_msg("", None)
    log_msg("=" * 80, None)


if __name__ == "__main__":
    main()

