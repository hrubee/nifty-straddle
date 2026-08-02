#!/usr/bin/env bash
# nifty-straddle one-line VPS installer
# Usage: curl -fsSL https://raw.githubusercontent.com/hrubee/nifty-straddle/main/install.sh |
#   TRADEJINI_DATA_API_KEY="xxx" TRADEJINI_PASSWORD="xxx" TRADEJINI_TOTP="xxx" bash

set -euo pipefail

REPO="https://github.com/hrubee/nifty-straddle.git"
INSTALL_DIR="${INSTALL_DIR:-/opt/nifty-straddle}"
SERVICE_NAME="nifty-straddle"
PYTHON="${PYTHON:-python3}"

# Required env vars for DATA FEED (premium WS) - needed for SHADOW mode
: "${TRADEJINI_DATA_API_KEY?TRADEJINI_DATA_API_KEY is required}"
: "${TRADEJINI_PASSWORD?TRADEJINI_PASSWORD is required}"
: "${TRADEJINI_TOTP?TRADEJINI_TOTP is required}"

# Optional: App credentials for ORDER EXECUTION (client OAuth flow)
# TRADEJINI_APP_KEY="xxx"
# TRADEJINI_APP_SECRET="xxx"
# TRADEJINI_REDIRECT_URI="https://yourdomain.com/callback"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
err() { log "ERROR: $*" >&2; exit 1; }

# Detect OS
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS=$ID
else
    err "Cannot detect OS"
fi

log "Installing nifty-straddle on $OS..."

# Install system deps
case $OS in
    ubuntu|debian)
        apt-get update -qq
        apt-get install -y -qq git $PYTHON python3-venv python3-pip ca-certificates
        ;;
    centos|rhel|fedora|rocky|almalinux)
        dnf install -y -q git $PYTHON python3-venv python3-pip ca-certificates || \
        yum install -y -q git $PYTHON python3-venv python3-pip ca-certificates
        ;;
    *)
        log "WARNING: Unknown OS $OS, assuming deps exist"
        ;;
esac

# Clone or update repo
if [[ -d "$INSTALL_DIR/.git" ]]; then
    log "Updating existing installation..."
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" reset --hard origin/main
else
    log "Cloning repository..."
    git clone --depth 1 "$REPO" "$INSTALL_DIR"
fi

# Create venv
log "Creating virtual environment..."
$PYTHON -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# Create systemd service
log "Creating systemd service..."
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<SVC
[Unit]
Description=Nifty Options Straddle (FaithfulStraddleEngine)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment=STRADDLE_DATA_SRC=tradejini
Environment=TRADEJINI_DATA_API_KEY=$TRADEJINI_DATA_API_KEY
Environment=TRADEJINI_PASSWORD=$TRADEJINI_PASSWORD
Environment=TRADEJINI_TOTP=$TRADEJINI_TOTP
Environment=PYTHONPATH=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m app.straddle_runner shadow
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$INSTALL_DIR

[Install]
WantedBy=multi-user.target
SVC

# Reload and enable
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

log "Installation complete!"
log ""
log "To start:  systemctl start $SERVICE_NAME"
log "To stop:   systemctl stop $SERVICE_NAME"
log "To logs:   journalctl -u $SERVICE_NAME -f"
log "To status: systemctl status $SERVICE_NAME"
log ""
log "Required for DATA FEED (premiums):"
log "  TRADEJINI_DATA_API_KEY  - Data account API key"
log "  TRADEJINI_PASSWORD      - Data account password"
log "  TRADEJINI_TOTP          - Data account TOTP secret"
log ""
log "Optional for ORDER EXECUTION (live trading):"
log "  TRADEJINI_APP_KEY       - OAuth app key"
log "  TRADEJINI_APP_SECRET    - OAuth app secret"
log "  TRADEJINI_REDIRECT_URI  - OAuth redirect URI"
log ""
log "The service runs in SHADOW mode (logs actions, places no orders)."
log "For live trading, add executor to app.straddle_runner and include app creds."
