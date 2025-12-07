# amBER Link Health Summarization Script

A comprehensive Python script for collecting and analyzing NVIDIA amBER (Advanced Mellanox BER) CSV data to generate detailed link health reports.

## Features

- **Automatic MST Management**: Installs and starts MST (Mellanox Software Tools) automatically
- **Data Collection**: Collects amBER data from all PCI devices using mlxlink
- **Detailed Analysis**: Generates comprehensive link health reports with:
  - FEC histogram analysis (all 16 bins)
  - BER (Bit Error Rate) metrics
  - Link events and recovery statistics
  - Cable and module information
  - Health verdict and recommendations
- **Smart Filenames**: Automatically includes device, port, and interface names in filenames
- **IP Link Mapping**: Maps MAC addresses to Linux interface names and states
- **Kernel Message Capture**: Automatically captures kernel messages (dmesg) with timestamps including milliseconds for link flap analysis

## Installation

The script can install MST automatically:

```bash
python3 amber_summarize.py --install-mst
```

## Usage

### Collect from all devices and generate reports

```bash
python3 amber_summarize.py --collect all
```

### Collect from specific device

```bash
python3 amber_summarize.py --collect /dev/mst/mt41692_pciconf0
```

### Process existing CSV files

```bash
python3 amber_summarize.py *.csv
```

### Install MST and collect data

```bash
python3 amber_summarize.py --install-mst --collect all
```

## Output

The script generates multiple output files:

### 1. CSV Data Files
- Format: `{prefix}_{device}_{port}_{interface}.csv`
- Example: `amber_data_mt416920_1_1_enp13s0f0np0.csv`

### 2. Detailed Analysis Logs
- Format: `{prefix}_{device}_{port}_{interface}.csv.log`
- Example: `amber_data_mt416920_1_1_enp13s0f0np0.csv.log`
- Contains: Complete link health analysis, FEC histograms, statistics, and verdicts

### 3. Kernel Message Logs (NEW)
- Format: `{prefix}_{device}_{port}_{interface}_kernel.log`
- Example: `amber_data_mt416920_1_1_enp13s0f0np0_kernel.log`
- Contains: All kernel messages (dmesg) with timestamps including milliseconds
- Useful for: Link flap analysis, troubleshooting link up/down events

## Requirements

- Python 3.6+
- Mellanox Firmware Tools (MFT) - can be installed automatically
- Linux with `ip` command for interface mapping

## Command Line Options

```
--collect [DEVICE]    Collect amBER data using mlxlink (use 'all' for all devices)
-p, --port PORT       Port number when using --collect option
-o, --output OUTPUT   Output CSV filename prefix (default: amber_data.csv)
--install-mst         Install MST (Mellanox Firmware Tools) if not already installed
```

## Example Output

### Detailed Analysis Log
The detailed log includes:
- Link/Protocol information
- BER/FEC metrics with detailed histogram
- SNR data
- Cable/Module details
- Link events and recovery statistics
- Health verdict and summary
- Additional statistics (FEC rates, uptime, etc.)
- All CSV fields (raw data dump)

### Kernel Message Log
The kernel log includes:
- Capture timestamp with millisecond precision: `Captured: 2025-12-07 04:16:21.214`
- All kernel messages with timestamps: `[Sat Dec  6 19:46:00 2025] mlx5_core ... Link down`
- Link up/down events for troubleshooting
- Complete dmesg output for analysis

### Viewing Kernel Logs

```bash
# View all kernel messages
cat amber_data_*_kernel.log

# Search for link events
grep -i 'link' amber_data_*_kernel.log

# View only link down events
grep -i 'link.*down' amber_data_*_kernel.log

# View recent link events
grep -i 'link' amber_data_*_kernel.log | tail -20
```

## License

See repository for license information.

