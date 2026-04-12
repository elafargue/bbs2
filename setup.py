# Minimal shim for old pip versions (< 21.3) that require setup.py for
# editable installs.  All actual metadata is in pyproject.toml.
from setuptools import setup
setup()
