"""
Agent configuration — edit SERVER_URL and optionally AGENT_TOKEN before deploying.
"""

import os

# URL of the Bretter-IMG server
SERVER_URL = os.environ.get("BRETTER_SERVER", "http://192.168.1.10:8000")

# Path to persist the machine UUID across reboots
AGENT_ID_FILE = os.environ.get("BRETTER_ID_FILE", r"C:\ProgramData\BretterIMG\machine_id.txt")

# Agent JWT token — set after running: GET /api/auth/agent-token/<machine_id>
AGENT_TOKEN = os.environ.get("BRETTER_AGENT_TOKEN", "")

# How often to poll the server (seconds)
POLL_INTERVAL = int(os.environ.get("BRETTER_POLL_INTERVAL", "10"))

# Temp path for captured/downloaded WIM files
TEMP_DIR = os.environ.get("BRETTER_TEMP", r"C:\ProgramData\BretterIMG\temp")

# WinPE staging area — where the restore script is written before reboot
WINPE_STAGE_DIR = os.environ.get("BRETTER_WINPE_STAGE", r"C:\ProgramData\BretterIMG\winpe")

AGENT_VERSION = "1.0.12"
