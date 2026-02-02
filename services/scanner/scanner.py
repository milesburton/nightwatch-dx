#!/usr/bin/env python3
"""
20M Band Scanner V3 - RTL-TCP Hub Architecture
Scans multiple frequencies via rtl_tcp, allowing simultaneous decoder operation
"""
import subprocess
import sys
import time
import signal
from datetime import datetime
import json
import socket
import struct
import numpy as np

# 20M Band Frequencies to Monitor (in MHz)
FREQUENCIES = [
    (14.000, "CW", 5),
    (14.030, "CW", 5),
    (14.070, "FT8", 10),
    (14.095, "PSK31", 5),
    (14.130, "CW/SSB", 5),
    (14.152, "SSB", 5),
    (14.200, "SSB", 5),
    (14.250, "SSB", 5),
    (14.300, "SSB", 5),
]

# Signal locking thresholds
LOCK_THRESHOLD = -6      # Lock on signals stronger than this
STRONG_THRESHOLD = -4    # Very strong signal
LOCK_DURATION_MIN = 30    # Minimum seconds to monitor locked signal
LOCK_DURATION_MAX = 300   # Maximum seconds to monitor locked signal

class RTLTCPScanner:
    """Lightweight RTL-TCP client for power scanning"""
    def __init__(self, host='127.0.0.1', port=1235):
        self.host = host
        self.port = port
        self.sock = None
        self.connected = False

    def connect(self):
        """Connect to rtl_tcp server"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((self.host, self.port))
            self.connected = True
            # Initialize with gain settings
            self.set_gain_mode(1)
            self.set_gain(250)  # 25 dB
            return True
        except Exception as e:
            print(f"Warning: Failed to connect to rtl_tcp: {e}")
            self.connected = False
            return False

    def set_frequency(self, freq_hz):
        """Set center frequency"""
        if not self.connected:
            return
        cmd = struct.pack('>BI', 0x01, int(freq_hz))
        self.sock.send(cmd)

    def set_sample_rate(self, rate):
        """Set sample rate"""
        if not self.connected:
            return
        cmd = struct.pack('>BI', 0x02, int(rate))
        self.sock.send(cmd)

    def set_gain_mode(self, manual=1):
        """Set gain mode (0=auto, 1=manual)"""
        if not self.connected:
            return
        cmd = struct.pack('>BI', 0x03, manual)
        self.sock.send(cmd)

    def set_gain(self, gain_tenths):
        """Set gain in tenths of dB (e.g., 250 = 25.0 dB)"""
        if not self.connected:
            return
        cmd = struct.pack('>BI', 0x04, int(gain_tenths))
        self.sock.send(cmd)

    def read_samples(self, num_samples):
        """Read IQ samples from rtl_tcp"""
        bytes_needed = num_samples * 2
        data = bytearray()

        while len(data) < bytes_needed:
            chunk = self.sock.recv(min(bytes_needed - len(data), 16384))
            if not chunk:
                raise ConnectionError("rtl_tcp connection closed")
            data.extend(chunk)

        # Convert to numpy array and normalize
        samples = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
        samples = (samples - 127.5) / 127.5

        # Create complex samples
        i_samples = samples[0::2]
        q_samples = samples[1::2]
        return i_samples + 1j * q_samples

    def scan_power(self, center_freq_hz, span_hz=10000, integration_time=0.5):
        """Scan power across a frequency range"""
        if not self.connected:
            return {'max_power': -100.0, 'avg_power': -100.0, 'detected': False, 'error': True}

        try:
            sample_rate = 2400000
            self.set_frequency(center_freq_hz)
            self.set_sample_rate(sample_rate)

            # Read samples
            num_samples = int(sample_rate * integration_time)
            samples = self.read_samples(num_samples)

            # Perform FFT
            fft_size = min(2048, num_samples)
            windowed = samples[:fft_size] * np.hanning(fft_size)
            fft_result = np.fft.fftshift(np.fft.fft(windowed))

            # Convert to power (dB)
            power = 20 * np.log10(np.abs(fft_result) + 1e-10)

            # Filter to span
            freq_bins = np.fft.fftshift(np.fft.fftfreq(fft_size, 1/sample_rate)) + center_freq_hz
            mask = np.abs(freq_bins - center_freq_hz) <= span_hz / 2
            power_in_span = power[mask]

            if len(power_in_span) == 0:
                return {'max_power': -100.0, 'avg_power': -100.0, 'detected': False}

            max_power = float(np.max(power_in_span))
            avg_power = float(np.mean(power_in_span))

            return {
                'max_power': max_power,
                'avg_power': avg_power,
                'signal_strength': max_power - avg_power,
                'detected': max_power > -6
            }
        except Exception as e:
            print(f"Warning: Error scanning {center_freq_hz}: {e}")
            return {'max_power': -100.0, 'avg_power': -100.0, 'detected': False, 'error': True}

    def close(self):
        """Close connection"""
        if self.sock:
            self.sock.close()
        self.connected = False

class BandScanner:
    def __init__(self, dwell_time=5):
        self.dwell_time = dwell_time
        self.running = False
        self.detections = {}
        self.scan_count = 0
        self.locked_freq = None
        self.lock_start_time = None
        self.rtl_scanner = RTLTCPScanner()

    def scan_frequency(self, freq_mhz, mode, duration):
        """Scan a single frequency for activity"""
        upconverted = int((freq_mhz + 125) * 1e6)
        return self.rtl_scanner.scan_power(upconverted, span_hz=10000, integration_time=0.5)

    def lock_and_monitor(self, freq_mhz, mode, initial_power):
        """Lock on frequency and monitor continuously"""
        self.locked_freq = freq_mhz
        self.lock_start_time = time.time()

        print()
        print("=" * 80)
        print(f"🔒 SIGNAL LOCK ENGAGED!")
        print(f"   Frequency: {freq_mhz:.3f} MHz ({mode})")
        print(f"   Initial Power: {initial_power:.1f} dB")
        print(f"   Lock Duration: {LOCK_DURATION_MIN}-{LOCK_DURATION_MAX}s")
        print("=" * 80)
        print()

        lock_log = []
        sample_count = 0

        while self.running:
            elapsed = time.time() - self.lock_start_time

            if elapsed > LOCK_DURATION_MAX:
                print(f"\n⏱️  Maximum lock duration reached ({LOCK_DURATION_MAX}s)")
                break

            result = self.scan_frequency(freq_mhz, mode, 2)
            sample_count += 1

            current_power = result.get('max_power', -100)
            signal_strength = result.get('signal_strength', 0)

            lock_log.append({
                'time': datetime.utcnow().isoformat(),
                'power': current_power,
                'strength': signal_strength
            })

            if current_power > STRONG_THRESHOLD:
                indicator = "🔴🔴🔴"
                status = "VERY STRONG"
            elif current_power > LOCK_THRESHOLD:
                indicator = "🔴🔴"
                status = "STRONG"
            elif current_power > -40:
                indicator = "🔴"
                status = "ACTIVE"
            else:
                indicator = "⚪"
                status = "QUIET"

            print(f"  [{sample_count:3d}] {elapsed:6.1f}s | {indicator} {current_power:6.1f} dB | {status:12s} | +{signal_strength:5.1f} dB")
            sys.stdout.flush()

            if current_power < -42 and elapsed > LOCK_DURATION_MIN:
                print(f"\n📉 Signal dropped below threshold. Releasing lock.")
                break

            time.sleep(2)

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"/tmp/signal_lock_{freq_mhz:.3f}MHz_{timestamp}.json"

        with open(filename, 'w') as f:
            json.dump({
                'frequency_mhz': freq_mhz,
                'mode': mode,
                'lock_start': datetime.fromtimestamp(self.lock_start_time).isoformat(),
                'lock_duration': elapsed,
                'samples': lock_log,
                'sample_count': sample_count,
                'max_power': max([s['power'] for s in lock_log]),
                'avg_power': sum([s['power'] for s in lock_log]) / len(lock_log)
            }, f, indent=2)

        print()
        print("=" * 80)
        print(f"🔓 LOCK RELEASED")
        print(f"   Monitored for: {elapsed:.1f}s ({sample_count} samples)")
        print(f"   Log saved to: {filename}")
        print("=" * 80)
        print()

        self.locked_freq = None
        self.lock_start_time = None

    def run_scan(self):
        """Continuous scanning loop with signal locking"""
        self.running = True

        print("=" * 80)
        print("20M BAND SCANNER V3 - RTL-TCP Hub Architecture")
        print("=" * 80)
        print(f"Monitoring {len(FREQUENCIES)} frequencies")
        print(f"Lock threshold: {LOCK_THRESHOLD} dB")
        print(f"Strong signal threshold: {STRONG_THRESHOLD} dB")
        print(f"Lock duration: {LOCK_DURATION_MIN}-{LOCK_DURATION_MAX}s")
        print("=" * 80)

        # Connect to rtl_tcp
        print("\nConnecting to rtl_tcp server...")
        if not self.rtl_scanner.connect():
            print("ERROR: Could not connect to rtl_tcp. Is rtl_tcp service running?")
            print("Try: sudo systemctl start rtl-tcp-server")
            return
        print("✓ Connected to rtl_tcp")
        print()

        try:
            while self.running:
                self.scan_count += 1

                print(f"\n[Scan #{self.scan_count}] {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
                print("-" * 80)

                for freq_mhz, mode, dwell in FREQUENCIES:
                    if not self.running:
                        break

                    freq_key = f"{freq_mhz:.3f}"
                    result = self.scan_frequency(freq_mhz, mode, dwell)

                    if freq_key not in self.detections:
                        self.detections[freq_key] = {
                            'mode': mode,
                            'total_scans': 0,
                            'detections': 0,
                            'locks': 0,
                            'last_seen': None,
                            'max_power_ever': -100
                        }

                    self.detections[freq_key]['total_scans'] += 1
                    current_power = result.get('max_power', -100)

                    if current_power > LOCK_THRESHOLD:
                        self.detections[freq_key]['detections'] += 1
                        self.detections[freq_key]['locks'] += 1
                        self.detections[freq_key]['last_seen'] = datetime.utcnow().isoformat()
                        self.detections[freq_key]['max_power_ever'] = max(
                            self.detections[freq_key]['max_power_ever'],
                            current_power
                        )

                        signal_strength = result.get('signal_strength', 0)
                        print(f"  🚨 {freq_mhz:.3f} MHz [{mode:6}] STRONG SIGNAL DETECTED!")
                        print(f"     Power: {current_power:.1f} dB (+{signal_strength:.1f} dB above noise)")
                        sys.stdout.flush()

                        self.lock_and_monitor(freq_mhz, mode, current_power)

                    elif result.get('detected'):
                        self.detections[freq_key]['detections'] += 1
                        signal_strength = result.get('signal_strength', 0)
                        print(f"  🟡 {freq_mhz:.3f} MHz [{mode:6}] active "
                              f"({current_power:.1f} dB, +{signal_strength:.1f} dB)")
                    else:
                        print(f"     {freq_mhz:.3f} MHz [{mode:6}] quiet ({current_power:.1f} dB)")

                    sys.stdout.flush()

                print("-" * 80)

                active_freqs = sum(1 for d in self.detections.values() if d['detections'] > 0)
                if active_freqs > 0:
                    print(f"\n📊 Activity Summary:")
                    for freq, data in sorted(self.detections.items()):
                        if data['detections'] > 0:
                            hit_rate = (data['detections'] / data['total_scans']) * 100
                            print(f"   {freq} MHz: {data['detections']}/{data['total_scans']} scans ({hit_rate:.0f}%), {data['locks'] or 0} locks")
        finally:
            self.rtl_scanner.close()

    def stop(self):
        """Stop scanning"""
        self.running = False
        self.rtl_scanner.close()

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"/tmp/band_scan_stats_{timestamp}.json"

        with open(filename, 'w') as f:
            json.dump({
                'scan_count': self.scan_count,
                'frequencies': self.detections
            }, f, indent=2)

        print(f"\n\nScan statistics saved to: {filename}")

def main():
    dwell_time = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    scanner = BandScanner(dwell_time)

    def signal_handler(sig, frame):
        print("\n\nStopping scanner...")
        scanner.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        scanner.run_scan()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        scanner.stop()

if __name__ == "__main__":
    main()
