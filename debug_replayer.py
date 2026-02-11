#!/usr/bin/env python3
"""Debug script to test replayer timestamp filtering."""

import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent / 'aiopslab-applications/static-replayers/openrca'))

from dataset_loader import OpenRCADatasetLoader
from time_mapper import TimeMapper

# Test configuration matching test_static_app.py
historical_fault_time_str = "2021-03-04T14:57:00"
historical_fault_time = datetime.fromisoformat(historical_fault_time_str)

print("=" * 80)
print("DEBUG REPLAYER TIMESTAMP FILTERING")
print("=" * 80)
print()

print("1. Historical Fault Time Conversion")
print(f"   Input: {historical_fault_time_str}")
print(f"   Parsed (naive): {historical_fault_time}")
print(f"   Timestamp (assuming UTC): {historical_fault_time.timestamp()}")
print()

# Convert from China time to UTC (subtract 8 hours)
if historical_fault_time.tzinfo is None:
    print(f"   Original (China/UTC+8): {historical_fault_time}")
    historical_fault_time_utc = historical_fault_time - timedelta(hours=8)
    print(f"   Converted to UTC: {historical_fault_time_utc}")
    print(f"   UTC Timestamp: {historical_fault_time_utc.timestamp()}")
else:
    historical_fault_time_utc = historical_fault_time

print()

# Initialize components
offset_minutes = 0
history_window_minutes = 30
simulation_start = datetime.now()

print("2. Time Mapper Initialization")
time_mapper = TimeMapper(
    historical_fault_time_utc,
    simulation_start,
    offset_minutes
)
print(f"   Historical fault: {time_mapper.historical_fault_time}")
print(f"   Simulation start: {time_mapper.simulation_start_time}")
print(f"   Offset: {time_mapper.offset_minutes} minutes")
print()

# Calculate time range for Phase 1
hist_start = historical_fault_time_utc + timedelta(minutes=offset_minutes - history_window_minutes)
hist_end = historical_fault_time_utc + timedelta(minutes=offset_minutes)

print("3. Phase 1 Time Range")
print(f"   Start: {hist_start} (timestamp: {hist_start.timestamp()})")
print(f"   End: {hist_end} (timestamp: {hist_end.timestamp()})")
print(f"   Window: {history_window_minutes} minutes")
print()

# Load dataset
dataset_path = Path("openrca_dataset/Bank")
print("4. Loading Dataset")
print(f"   Path: {dataset_path}")
loader = OpenRCADatasetLoader(dataset_path)
print()

# Load data in time range
print("5. Loading Telemetry in Time Range")
telemetry_data = loader.load_telemetry_in_time_range(
    hist_start,
    hist_end,
    ['trace', 'log', 'metric']
)

print()
print("6. Results")
for ttype, df in telemetry_data.items():
    print(f"   {ttype}: {len(df)} rows")
    if len(df) > 0:
        print(f"      Timestamp range: {df['timestamp'].min():.0f} to {df['timestamp'].max():.0f}")

if not any(len(df) > 0 for df in telemetry_data.values()):
    print()
    print("⚠️  NO DATA LOADED!")
    print()
    print("Possible issues:")
    print("  - Timestamp filtering is too restrictive")
    print("  - Timezone conversion is incorrect")
    print("  - Dataset structure doesn't match expected format")
else:
    print()
    print("✓ Data loaded successfully!")
