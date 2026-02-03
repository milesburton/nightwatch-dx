"""
Sanity tests to ensure test infrastructure works
"""
import pytest


def test_python_version():
    """Test that we're running a supported Python version"""
    import sys
    assert sys.version_info >= (3, 11), "Python 3.11+ required"


def test_imports():
    """Test that we can import standard libraries"""
    import socket
    import threading
    import json
    assert True


def test_numpy_available():
    """Test that numpy is available"""
    try:
        import numpy as np
        assert np.__version__
    except ImportError:
        pytest.skip("numpy not installed")


def test_scipy_available():
    """Test that scipy is available"""
    try:
        import scipy
        assert scipy.__version__
    except ImportError:
        pytest.skip("scipy not installed")


def test_basic_math():
    """Basic sanity test"""
    assert 1 + 1 == 2
    assert 2 * 2 == 4
    assert 10 / 2 == 5


def test_fixture(sample_test_data):
    """Test that fixtures work"""
    assert sample_test_data['test_value'] == 42
    assert sample_test_data['test_string'] == 'hello'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
