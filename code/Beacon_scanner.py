#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import time
import requests
import re

# Configuration
SCANNER_ID = "scanner_2"
SERVER_URL = "http://10.161.33.254:5000/api/upload"
TARGET_MACS = [
    "52:0A:25:08:00:C1"
]
SCAN_DURATION = 5
UPLOAD_INTERVAL = 10


def single_scan_round():
    beacons = {}
    subprocess.run(["bluetoothctl", "scan", "off"], capture_output=True)
    time.sleep(0.5)

    try:
        result = subprocess.run(
            ["timeout", "3", "bluetoothctl", "scan", "on"],
            capture_output=True, text=True
        )

        current_device = None
        for line in result.stdout.split('\n'):
            if "Device" in line:
                for mac in TARGET_MACS:
                    if mac in line:
                        current_device = mac
                        if mac not in beacons:
                            beacons[mac] = -70

            if current_device and "RSSI" in line:
                rssi_match = re.search(r"RSSI[:\s]*(-?\d+)", line)
                if rssi_match:
                    rssi = int(rssi_match.group(1))
                    beacons[current_device] = rssi

    except Exception as e:
        print(f"Single scan error: {e}")

    subprocess.run(["bluetoothctl", "scan", "off"], capture_output=True)
    return beacons


def scan_with_rssi():
    print("\n===== [Scan Started] =====")
    beacons = {}

    all_scan_results = []
    for round_num in range(3):
        print(f"Round {round_num + 1} scanning...")
        round_beacons = single_scan_round()
        all_scan_results.append(round_beacons)
        if round_num < 2:
            time.sleep(1)

    for round_result in all_scan_results:
        for mac, rssi in round_result.items():
            if mac not in beacons:
                beacons[mac] = []
            beacons[mac].append(rssi)

    averaged_beacons = {}
    for mac, rssi_list in beacons.items():
        if len(rssi_list) >= 2:
            sorted_rssi = sorted(rssi_list)
            median_rssi = sorted_rssi[len(sorted_rssi) // 2]
            averaged_beacons[mac] = median_rssi
            print(f"Beacon {mac} median RSSI: {median_rssi} (from {len(rssi_list)} measurements)")
        elif len(rssi_list) == 1:
            averaged_beacons[mac] = rssi_list[0]
            print(f"Beacon {mac} RSSI: {rssi_list[0]} (single measurement)")

    print(f"===== [Scan Finished] =====")
    return averaged_beacons


def upload_data(beacons):
    if not beacons:
        print("No beacons to upload, skipping.")
        return

    print("\n===== [Upload Started] =====")

    success_count = 0
    for mac, rssi in beacons.items():
        data = {
            "scanner_id": SCANNER_ID,
            "beacon_mac": mac,
            "rssi": rssi
        }

        try:
            response = requests.post(SERVER_URL, json=data, timeout=5)
            if response.status_code == 200:
                print(f"? {mac} (RSSI: {rssi}): Upload successful")
                success_count += 1
            else:
                print(f"? {mac}: Upload failed - {response.status_code}")
        except Exception as e:
            print(f"? {mac}: Upload error - {e}")

    print(f"===== [Upload Finished] =====")
    print(f"Successfully uploaded {success_count}/{len(beacons)} beacons")


if __name__ == "__main__":
    print(f"Beacon scanner started (ID: {SCANNER_ID})")
    print(f"Target MACs: {TARGET_MACS}")

    # Initialize
    subprocess.run(["sudo", "systemctl", "restart", "bluetooth"], capture_output=True)
    time.sleep(3)

    scan_count = 0
    try:
        while True:
            scan_count += 1
            print(f"\nScan #{scan_count}")

            beacons = scan_with_rssi()
            upload_data(beacons)

            print("-" * 50)
            time.sleep(UPLOAD_INTERVAL)

    except KeyboardInterrupt:
        print(f"\nScanner stopped after {scan_count} scans")
        subprocess.run(["bluetoothctl", "scan", "off"], capture_output=True)