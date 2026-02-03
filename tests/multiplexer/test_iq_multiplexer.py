"""
Tests for I/Q Multiplexer
"""
import pytest
import socket
import threading
import time
from unittest.mock import Mock, MagicMock, patch
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../services/multiplexer'))

from iq_multiplexer import IQMultiplexer


class TestIQMultiplexer:
    """Test suite for IQMultiplexer class"""

    def test_init(self):
        """Test multiplexer initialization"""
        mux = IQMultiplexer('localhost', 1234, 1235)
        assert mux.rtl_tcp_host == 'localhost'
        assert mux.rtl_tcp_port == 1234
        assert mux.listen_port == 1235
        assert mux.rtl_tcp_sock is None
        assert mux.server_sock is None
        assert mux.clients == []
        assert mux.running is False

    def test_init_defaults(self):
        """Test multiplexer initialization with defaults"""
        mux = IQMultiplexer()
        assert mux.rtl_tcp_host == '127.0.0.1'
        assert mux.rtl_tcp_port == 1234
        assert mux.listen_port == 1235

    @patch('socket.socket')
    def test_connect_to_rtl_tcp_success(self, mock_socket):
        """Test successful connection to rtl_tcp"""
        mock_sock = Mock()
        mock_socket.return_value = mock_sock

        mux = IQMultiplexer()
        result = mux.connect_to_rtl_tcp()

        assert result is True
        assert mux.rtl_tcp_sock == mock_sock
        mock_sock.connect.assert_called_once_with(('127.0.0.1', 1234))

    @patch('socket.socket')
    def test_connect_to_rtl_tcp_failure(self, mock_socket):
        """Test failed connection to rtl_tcp"""
        mock_sock = Mock()
        mock_sock.connect.side_effect = ConnectionRefusedError()
        mock_socket.return_value = mock_sock

        mux = IQMultiplexer()
        result = mux.connect_to_rtl_tcp()

        assert result is False

    @patch('socket.socket')
    def test_start_server_success(self, mock_socket):
        """Test successful server start"""
        mock_sock = Mock()
        mock_socket.return_value = mock_sock

        mux = IQMultiplexer()
        result = mux.start_server()

        assert result is True
        assert mux.server_sock == mock_sock
        mock_sock.bind.assert_called_once_with(('127.0.0.1', 1235))
        mock_sock.listen.assert_called_once_with(10)

    @patch('socket.socket')
    def test_start_server_failure(self, mock_socket):
        """Test failed server start"""
        mock_sock = Mock()
        mock_sock.bind.side_effect = OSError()
        mock_socket.return_value = mock_sock

        mux = IQMultiplexer()
        result = mux.start_server()

        assert result is False

    def test_client_management(self):
        """Test client list management"""
        mux = IQMultiplexer()

        # Add mock clients
        client1 = {'socket': Mock(), 'addr': ('127.0.0.1', 12345)}
        client2 = {'socket': Mock(), 'addr': ('127.0.0.1', 12346)}

        mux.clients.append(client1)
        mux.clients.append(client2)

        assert len(mux.clients) == 2
        assert client1 in mux.clients
        assert client2 in mux.clients

    def test_stats_tracking(self):
        """Test statistics tracking"""
        mux = IQMultiplexer()

        assert mux.bytes_received == 0
        assert mux.bytes_sent == 0

        # Simulate data transfer
        mux.bytes_received = 1000
        mux.bytes_sent = 2000

        assert mux.bytes_received == 1000
        assert mux.bytes_sent == 2000


class TestMultiplexerIntegration:
    """Integration tests for multiplexer"""

    @pytest.mark.integration
    def test_main_function_help(self, capsys):
        """Test main function with help flag"""
        import iq_multiplexer

        sys.argv = ['iq_multiplexer.py', '--help']

        with pytest.raises(SystemExit) as exc_info:
            iq_multiplexer.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert 'Usage:' in captured.out


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
