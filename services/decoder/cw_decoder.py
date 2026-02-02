#!/usr/bin/env python3
"""
CW Decoder for RTL-TCP
Connects to rtl_tcp hub and decodes Morse code signals
"""
import socket
import struct
import numpy as np
from scipy import signal
from collections import deque
import time
from datetime import datetime, timezone

# Morse code dictionary
MORSE_CODE = {
    '.-': 'A', '-...': 'B', '-.-.': 'C', '-..': 'D', '.': 'E',
    '..-.': 'F', '--.': 'G', '....': 'H', '..': 'I', '.---': 'J',
    '-.-': 'K', '.-..': 'L', '--': 'M', '-.': 'N', '---': 'O',
    '.--.': 'P', '--.-': 'Q', '.-.': 'R', '...': 'S', '-': 'T',
    '..-': 'U', '...-': 'V', '.--': 'W', '-..-': 'X', '-.--': 'Y',
    '--..': 'Z',
    '.----': '1', '..---': '2', '...--': '3', '....-': '4', '.....': '5',
    '-....': '6', '--...': '7', '---..': '8', '----.': '9', '-----': '0',
    '.-.-.-': '.', '--..--': ',', '..--..': '?', '.----.': "'", '-.-.--': '!',
    '-..-.': '/', '-.--.': '(', '-.--.-': ')', '.-...': '&', '---...': ':',
    '-.-.-.': ';', '-...-': '=', '.-.-.': '+', '-....-': '-', '..--.-': '_',
    '.-..-.': '"', '...-..-': '$', '.--.-.': '@'
}

class CWDecoder:
    def __init__(self, host='127.0.0.1', port=1235, frequency_mhz=14.000, wpm=20):
        self.host = host
        self.port = port
        self.frequency = int((frequency_mhz + 125) * 1e6)  # Upconverted frequency
        self.sample_rate = 240000  # 240 kHz sample rate
        self.sock = None
        self.running = False
        
        # CW timing parameters (based on WPM)
        self.wpm = wpm
        self.dit_duration = 1.2 / wpm  # Duration of a dit in seconds
        self.tolerance = 0.5  # 50% tolerance for timing
        
        # Decoder state
        self.current_symbol = ""
        self.last_tone_time = None
        self.tone_start = None
        self.is_tone_on = False
        
        print(f"CW Decoder initialized")
        print(f"Frequency: {frequency_mhz:.3f} MHz (upconverted: {self.frequency/1e6:.3f} MHz)")
        print(f"Speed estimate: {wpm} WPM")
        print(f"Dit duration: {self.dit_duration*1000:.1f} ms")
        
    def connect(self):
        """Connect to rtl_tcp server"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((self.host, self.port))
            
            # Configure rtl_tcp
            self.set_frequency(self.frequency)
            self.set_sample_rate(self.sample_rate)
            self.set_gain_mode(1)
            self.set_gain(250)  # 25 dB
            
            print(f"✓ Connected to rtl_tcp at {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f"✗ Failed to connect to rtl_tcp: {e}")
            return False
    
    def set_frequency(self, freq_hz):
        """Set center frequency"""
        cmd = struct.pack('>BI', 0x01, int(freq_hz))
        self.sock.send(cmd)
    
    def set_sample_rate(self, rate):
        """Set sample rate"""
        cmd = struct.pack('>BI', 0x02, int(rate))
        self.sock.send(cmd)
    
    def set_gain_mode(self, manual=1):
        """Set gain mode"""
        cmd = struct.pack('>BI', 0x03, manual)
        self.sock.send(cmd)
    
    def set_gain(self, gain_tenths):
        """Set gain in tenths of dB"""
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
        
        # Convert to complex IQ samples
        samples = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
        samples = (samples - 127.5) / 127.5
        i_samples = samples[0::2]
        q_samples = samples[1::2]
        return i_samples + 1j * q_samples
    
    def detect_envelope(self, samples):
        """Detect CW envelope using magnitude"""
        # Calculate magnitude (envelope)
        envelope = np.abs(samples)
        
        # Low-pass filter to smooth envelope
        b, a = signal.butter(3, 0.1)
        smoothed = signal.filtfilt(b, a, envelope)
        
        return smoothed
    
    def decode_morse(self, morse_str):
        """Decode Morse code string to text"""
        morse_str = morse_str.strip()
        if morse_str in MORSE_CODE:
            return MORSE_CODE[morse_str]
        return None
    
    def process_tone(self, duration):
        """Process a tone on/off event"""
        # Classify as dit or dah based on duration
        dit_min = self.dit_duration * (1 - self.tolerance)
        dit_max = self.dit_duration * (1 + self.tolerance)
        dah_min = self.dit_duration * 3 * (1 - self.tolerance)
        dah_max = self.dit_duration * 3 * (1 + self.tolerance)
        
        if dit_min <= duration <= dit_max:
            return '.'
        elif dah_min <= duration <= dah_max:
            return '-'
        return None
    
    def process_space(self, duration):
        """Process a space (silence) event"""
        # Inter-element space (between dots/dashes): ~1 dit
        # Inter-character space: ~3 dits
        # Inter-word space: ~7 dits
        
        char_space_min = self.dit_duration * 2.5
        word_space_min = self.dit_duration * 5
        
        if duration >= word_space_min:
            return 'WORD'
        elif duration >= char_space_min:
            return 'CHAR'
        return 'ELEMENT'
    
    def run(self):
        """Main decoder loop"""
        self.running = True
        print("\n" + "="*70)
        print("CW DECODER RUNNING - Listening for Morse code...")
        print("="*70 + "\n")
        
        chunk_size = int(self.sample_rate * 0.05)  # 50ms chunks
        threshold = 0.15  # Envelope threshold for tone detection
        
        last_print = time.time()
        decoded_text = ""
        
        try:
            while self.running:
                # Read samples
                samples = self.read_samples(chunk_size)
                
                # Detect envelope
                envelope = self.detect_envelope(samples)
                avg_envelope = np.mean(envelope)
                
                current_time = time.time()
                
                # Tone detection
                if avg_envelope > threshold:
                    if not self.is_tone_on:
                        # Tone just turned on
                        self.is_tone_on = True
                        self.tone_start = current_time
                        
                        # Process the silence that just ended
                        if self.last_tone_time is not None:
                            silence_duration = current_time - self.last_tone_time
                            space_type = self.process_space(silence_duration)
                            
                            if space_type == 'CHAR' and self.current_symbol:
                                # Decode the character
                                char = self.decode_morse(self.current_symbol)
                                if char:
                                    decoded_text += char
                                    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {self.current_symbol:8} → {char}")
                                self.current_symbol = ""
                            
                            elif space_type == 'WORD':
                                # Word space
                                if self.current_symbol:
                                    char = self.decode_morse(self.current_symbol)
                                    if char:
                                        decoded_text += char
                                        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {self.current_symbol:8} → {char}")
                                    self.current_symbol = ""
                                decoded_text += " "
                                print(f"\n>>> {decoded_text}")
                                print()
                else:
                    if self.is_tone_on:
                        # Tone just turned off
                        self.is_tone_on = False
                        self.last_tone_time = current_time
                        
                        # Process the tone that just ended
                        tone_duration = current_time - self.tone_start
                        symbol = self.process_tone(tone_duration)
                        
                        if symbol:
                            self.current_symbol += symbol
                
                # Periodic status update
                if current_time - last_print > 10:
                    status = "TONE ON" if self.is_tone_on else "LISTENING"
                    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Status: {status} | Signal: {avg_envelope:.3f} | Buffer: {self.current_symbol or '(empty)'}")
                    last_print = current_time
                    
        except KeyboardInterrupt:
            print("\n\nDecoder stopped by user")
        finally:
            self.close()
    
    def close(self):
        """Close connection"""
        self.running = False
        if self.sock:
            self.sock.close()
        print("\nConnection closed")

def main():
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: cw_decoder_rtltcp.py <frequency_MHz> [WPM]")
        print("  frequency_MHz: HF frequency (e.g., 14.000)")
        print("  WPM: Words per minute estimate (default: 20)")
        print("\nExample: python3 cw_decoder_rtltcp.py 14.000 20")
        sys.exit(1)
    
    frequency = float(sys.argv[1])
    wpm = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    
    decoder = CWDecoder(frequency_mhz=frequency, wpm=wpm)
    
    if not decoder.connect():
        sys.exit(1)
    
    decoder.run()

if __name__ == "__main__":
    main()
