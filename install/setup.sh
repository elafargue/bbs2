#!/usr/bin/env bash
# install/setup.sh — install BBS2 on a Linux system
# Run as root: sudo bash install/setup.sh
set -euo pipefail

INSTALL_DIR=/opt/bbs2
BBS_USER=bbs

echo "=== BBS2 Setup ==="

# 1. Create bbs system user
if ! id "$BBS_USER" &>/dev/null; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$BBS_USER"
    echo "Created user: $BBS_USER"
else
    echo "User $BBS_USER already exists"
fi

# 2. Add bbs user to dialout group (serial port access for KISS TNC)
usermod -aG dialout "$BBS_USER" || true

# 3. Create installation directory
mkdir -p "$INSTALL_DIR"/{config,data,static}
cp -r bbs server "$INSTALL_DIR/"
cp pyproject.toml "$INSTALL_DIR/"

# 4. Create Python virtual environment
python3 -m venv "$INSTALL_DIR/venv"
# Debian/Raspberry Pi OS venvs do not include pip by default — bootstrap it.
"$INSTALL_DIR/venv/bin/python3" -m ensurepip --upgrade
"$INSTALL_DIR/venv/bin/python3" -m pip install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -e "$INSTALL_DIR"

# 5. Copy example config if no config exists
if [ ! -f "$INSTALL_DIR/config/bbs.yaml" ]; then
    cp config/bbs.yaml.example "$INSTALL_DIR/config/bbs.yaml"
    echo ""
    echo "*** Edit $INSTALL_DIR/config/bbs.yaml before starting the BBS ***"
    echo "    Set: bbs.callsign, web.secret_key"
    echo "    Then run: sudo -u $BBS_USER $INSTALL_DIR/venv/bin/bbs2 --config $INSTALL_DIR/config/bbs.yaml --set-sysop-password"
fi

# 6. Set permissions
chown -R "$BBS_USER:$BBS_USER" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR/config"
chmod 700 "$INSTALL_DIR/data"

# 7. Install frontend (if built)
if [ -d static ] && [ "$(ls -A static)" ]; then
    cp -r static/* "$INSTALL_DIR/static/"
    echo "Frontend assets copied to $INSTALL_DIR/static"
else
    echo "NOTE: Frontend not built. Run 'cd vue-app && npm install && npm run build' first."
fi

# 8. Install and enable systemd service
install -m 644 install/bbs2.service /etc/systemd/system/bbs2.service
systemctl daemon-reload
systemctl enable bbs2.service
echo "Systemd service installed and enabled"

echo ""
echo "=== Setup complete ==="
echo "Start the BBS with: systemctl start bbs2"
echo "View logs with:     journalctl -u bbs2 -f"
