#!/usr/bin/env python3
"""
I/Q Sample Multiplexer for RTL-TCP
Connects to rtl_tcp and rebroadcasts samples to multiple clients
"""
import socket
import threading
import struct
import time
import sys
from datetime import datetime, timezone

class IQMultiplexer:
    def __init__(self, rtl_tcp_host='127.0.0.1', rtl_tcp_port=1234, listen_port=1235):
        self.rtl_tcp_host = rtl_tcp_host
        self.rtl_tcp_port = rtl_tcp_port
        self.listen_port = listen_port
        
        self.rtl_tcp_sock = None
        self.server_sock = None
        self.clients = []
        self.clients_lock = threading.Lock()
        self.running = False
        
        # Stats
        self.bytes_received = 0
        self.bytes_sent = 0
        self.start_time = None
        
    def connect_to_rtl_tcp(self):
        """Connect to rtl_tcp server"""
        try:
            self.rtl_tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.rtl_tcp_sock.connect((self.rtl_tcp_host, self.rtl_tcp_port))
            print(f"✓ Connected to rtl_tcp at {self.rtl_tcp_host}:{self.rtl_tcp_port}")
            return True
        except Exception as e:
            print(f"✗ Failed to connect to rtl_tcp: {e}")
            return False
    
    def start_server(self):
        """Start TCP server for clients"""
        try:
            self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_sock.bind(('127.0.0.1', self.listen_port))
            self.server_sock.listen(10)
            print(f"✓ Multiplexer listening on port {self.listen_port}")
            return True
        except Exception as e:
            print(f"✗ Failed to start server: {e}")
            return False
    
    def accept_clients(self):
        """Accept new client connections"""
        while self.running:
            try:
                self.server_sock.settimeout(1.0)
                try:
                    client_sock, client_addr = self.server_sock.accept()
                    with self.clients_lock:
                        self.clients.append({
                            'socket': client_sock,
                            'addr': client_addr,
                            'connected_at': time.time(),
                            'bytes_sent': 0
                        })
                    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Client connected: {client_addr} (total: {len(self.clients)})")
                except socket.timeout:
                    continue
            except Exception as e:
                if self.running:
                    print(f"Error accepting client: {e}")
    
    def handle_client_commands(self, client_info):
        """Handle commands from a client and forward to rtl_tcp"""
        client_sock = client_info['socket']
        client_sock.setblocking(False)
        
        while self.running:
            try:
                # Check if client sent any commands
                try:
                    cmd_data = client_sock.recv(5, socket.MSG_DONTWAIT)
                    if cmd_data and len(cmd_data) == 5:
                        # Forward command to rtl_tcp
                        self.rtl_tcp_sock.send(cmd_data)
                        cmd_type = cmd_data[0]
                        cmd_val = struct.unpack('>I', cmd_data[1:5])[0]
                        print(f"  Client {client_info['addr']} sent command: type={cmd_type}, value={cmd_val}")
                except BlockingIOError:
                    pass
                
                time.sleep(0.1)
            except Exception as e:
                break
    
    def broadcast_samples(self):
        """Read from rtl_tcp and broadcast to all clients"""
        chunk_size = 16384  # 8192 IQ samples
        
        while self.running:
            try:
                # Read from rtl_tcp
                data = self.rtl_tcp_sock.recv(chunk_size)
                if not data:
                    print("✗ rtl_tcp connection closed")
                    self.running = False
                    break
                
                self.bytes_received += len(data)
                
                # Broadcast to all clients
                with self.clients_lock:
                    disconnected = []
                    
                    for client_info in self.clients:
                        try:
                            client_info['socket'].sendall(data)
                            client_info['bytes_sent'] += len(data)
                            self.bytes_sent += len(data)
                        except Exception as e:
                            # Client disconnected
                            disconnected.append(client_info)
                    
                    # Remove disconnected clients
                    for client_info in disconnected:
                        self.clients.remove(client_info)
                        try:
                            client_info['socket'].close()
                        except:
                            pass
                        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Client disconnected: {client_info['addr']} (total: {len(self.clients)})")
                
            except Exception as e:
                if self.running:
                    print(f"Error broadcasting: {e}")
                    self.running = False
    
    def print_stats(self):
        """Print statistics periodically"""
        while self.running:
            time.sleep(10)
            
            if not self.running:
                break
            
            elapsed = time.time() - self.start_time
            rx_rate = (self.bytes_received / elapsed) / 1024 / 1024  # MB/s
            tx_rate = (self.bytes_sent / elapsed) / 1024 / 1024  # MB/s
            
            with self.clients_lock:
                num_clients = len(self.clients)
            
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Stats: {num_clients} clients | RX: {rx_rate:.2f} MB/s | TX: {tx_rate:.2f} MB/s")
    
    def run(self):
        """Main multiplexer loop"""
        print("\n" + "="*70)
        print("I/Q MULTIPLEXER")
        print("="*70)

        # Retry connection to rtl_tcp with backoff
        max_retries = 10
        for attempt in range(max_retries):
            if self.connect_to_rtl_tcp():
                break
            if attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 30)  # Exponential backoff, max 30s
                print(f"  Retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
        else:
            print(f"✗ Failed to connect after {max_retries} attempts")
            return

        if not self.start_server():
            return
        
        self.running = True
        self.start_time = time.time()
        
        # Start threads
        accept_thread = threading.Thread(target=self.accept_clients, daemon=True)
        broadcast_thread = threading.Thread(target=self.broadcast_samples, daemon=True)
        stats_thread = threading.Thread(target=self.print_stats, daemon=True)
        
        accept_thread.start()
        broadcast_thread.start()
        stats_thread.start()
        
        print("\n✓ Multiplexer running")
        print(f"  Connect clients to: 127.0.0.1:{self.listen_port}")
        print(f"  Press Ctrl+C to stop\n")
        
        try:
            # Keep main thread alive
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\nStopping multiplexer...")
            self.running = False
        
        # Cleanup
        with self.clients_lock:
            for client_info in self.clients:
                try:
                    client_info['socket'].close()
                except:
                    pass
        
        if self.rtl_tcp_sock:
            self.rtl_tcp_sock.close()
        
        if self.server_sock:
            self.server_sock.close()
        
        print("Multiplexer stopped")

def main():
    if len(sys.argv) > 1 and sys.argv[1] in ['-h', '--help']:
        print("Usage: iq_multiplexer.py [rtl_tcp_host] [rtl_tcp_port] [listen_port]")
        print("  rtl_tcp_host: rtl_tcp server host (default: 127.0.0.1)")
        print("  rtl_tcp_port: rtl_tcp server port (default: 1234)")
        print("  listen_port: port to listen on for clients (default: 1235)")
        print("\nExample: python3 iq_multiplexer.py 127.0.0.1 1234 1235")
        sys.exit(0)
    
    rtl_tcp_host = sys.argv[1] if len(sys.argv) > 1 else '127.0.0.1'
    rtl_tcp_port = int(sys.argv[2]) if len(sys.argv) > 2 else 1234
    listen_port = int(sys.argv[3]) if len(sys.argv) > 3 else 1235
    
    mux = IQMultiplexer(rtl_tcp_host, rtl_tcp_port, listen_port)
    mux.run()

if __name__ == "__main__":
    main()
