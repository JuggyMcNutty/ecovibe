#!/bin/bash
# Build script for OVH Flash Sale Monitor
#
# IMPORTANT: This script requires Python 3.10, 3.11, 3.12, or 3.13.
# Python 3.14 is NOT supported by PyInstaller. If you have 3.14,
# the script will fail to build a working binary.
#
# For Python 3.14 users: Run with `python run.py` instead.
#
# Usage:
#   ./build.sh              # Uses default python3
#   ./build.sh python3.12   # Use specific Python version

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${1:-python3}"

echo "Building OVH Flash Sale Monitor"
echo "Using Python: $($PYTHON --version)"
echo ""

# Check Python version
PYTHON_VERSION=$($PYTHON -c 'import sys; print(sys.version_info[1])')
PYTHON_MAJOR=$($PYTHON -c 'import sys; print(sys.version_info[0])')

if [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_VERSION" -ge 14 ]; then
    echo "ERROR: Python 3.14+ detected."
    echo ""
    echo "PyInstaller does NOT yet support Python 3.14."
    echo ""
    echo "You have two options:"
    echo ""
    echo "1. Run directly without building a binary:"
    echo "   ./run.py"
    echo ""
    echo "2. Install an older Python version and build:"
    echo "   dnf install python3.12 python3.12-devel"
    echo "   ./build.sh python3.12"
    echo ""
    echo "The source code works fine - it's just the binary build that requires older Python."
    exit 1
fi

# Clean previous builds
echo "Cleaning previous builds..."
rm -rf build dist

# Install dependencies in a temporary venv
echo "Setting up build environment..."
$PYTHON -m venv venv_build
source venv_build/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller

# Copy static files
echo "Copying static files..."
mkdir -p dist/ovh-flash-monitor/static/js dist/ovh-flash-monitor/templates
cp static/js/app.js dist/ovh-flash-monitor/static/js/
cp templates/index.html dist/ovh-flash-monitor/templates/

# Build with PyInstaller
echo "Building with PyInstaller..."
pyinstaller ovh-flash-monitor.spec --clean

# Copy static files to final location
cp -r static/js dist/ovh-flash-monitor/static/
cp -r templates dist/ovh-flash-monitor/

# Cleanup
deactivate
rm -rf venv_build

echo ""
echo "Build complete!"
echo "===================="
echo ""
echo "Binary: dist/ovh-flash-monitor/ovh-flash-monitor"
echo ""
echo "To run the binary:"
echo "  cd dist/ovh-flash-monitor"
echo "  export OVH_APPLICATION_KEY=your_key"
echo "  export OVH_APPLICATION_SECRET=your_secret"
echo "  export OVH_CONSUMER_KEY=your_consumer_key"
echo "  ./ovh-flash-monitor"
echo ""
echo "Then open http://localhost:8000 in your browser."
