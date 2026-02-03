"""
Pytest configuration and fixtures
"""
import pytest
import sys
import os

# Add services directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../services/multiplexer'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../services/scanner'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../services/decoder'))


@pytest.fixture
def sample_test_data():
    """Sample test data fixture"""
    return {
        'test_value': 42,
        'test_string': 'hello'
    }
