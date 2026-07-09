import pytest
import subprocess
from unittest.mock import patch
from main import _clear_port

@patch("subprocess.run")
def test_clear_port_valid(mock_run):
    _clear_port(8080)
    mock_run.assert_called_once_with(["fuser", "-k", "8080/tcp"], capture_output=True)

@patch("subprocess.run")
def test_clear_port_valid_str(mock_run):
    _clear_port("8080")
    mock_run.assert_called_once_with(["fuser", "-k", "8080/tcp"], capture_output=True)

@patch("subprocess.run")
def test_clear_port_invalid_str(mock_run):
    _clear_port("8080; echo 'hacked'")
    mock_run.assert_not_called()

@patch("subprocess.run")
def test_clear_port_out_of_range_low(mock_run):
    _clear_port(0)
    mock_run.assert_not_called()
    _clear_port(-10)
    mock_run.assert_not_called()

@patch("subprocess.run")
def test_clear_port_out_of_range_high(mock_run):
    _clear_port(65536)
    mock_run.assert_not_called()
    _clear_port(70000)
    mock_run.assert_not_called()
