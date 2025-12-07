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

The script generates detailed log files with the format:
- `{prefix}_{device}_{port}_{interface}.csv.log`

Example: `amber_data_mt416920_1_1_enp13s0f0np0.csv.log`

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

The detailed log includes:
- Link/Protocol information
- BER/FEC metrics with detailed histogram
- SNR data
- Cable/Module details
- Link events and recovery statistics
- Health verdict and summary
- Additional statistics (FEC rates, uptime, etc.)
- All CSV fields (raw data dump)

## License

See repository for license information.

