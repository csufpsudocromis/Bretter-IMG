# 🖥 Bretter-IMG — Remote Imaging Console

A self-hosted, open-source replacement for Symantec Ghost Console.
Remotely capture, store, and deploy Windows disk images from a web dashboard.

---

## Features

| Feature | Description |
|---|---|
| **Machine Inventory** | Agents auto-register; see hostname, IP, MAC, OS, CPU, RAM, disks |
| **Image Capture** | Trigger DISM WIM capture of a live Windows drive remotely |
| **Image Deploy** | Push a WIM to one or many machines; boots into WinPE to apply |
| **Image Library** | Upload, browse, and manage `.wim`/`.img` files with checksums |
| **Job Queue** | Schedule jobs immediately or at a future time; see real-time log output |
| **Bulk Operations** | Select multiple machines → Capture / Deploy / Reboot / Shutdown |
| **Secure Auth** | JWT for console users; separate long-lived agent tokens per machine |
| **User Accounts + SMB Access** | Admins create named user accounts with matching access to the image share |
| **WinPE Builder** | PowerShell script builds a bootable ISO/USB for bare-metal imaging |

---

## Architecture

```
┌─────────────────────────────────────┐
│          Web Browser                │
│     React + TanStack Query          │
│   (auto-refreshes every 10s)        │
└──────────────┬──────────────────────┘
               │ HTTPS
┌──────────────▼──────────────────────┐
│       FastAPI Server (Python)       │
│  /api/machines  /api/images         │
│  /api/jobs      /api/auth           │
│  SQLite DB    images-store/         │
└──────────────┬──────────────────────┘
               │ HTTP polling (every 10s)
    ┌──────────┴──────────┐
    ▼                     ▼
┌────────────┐     ┌────────────┐
│  Agent.exe │     │  Agent.exe │  (runs as Windows Service)
│  PC-001    │     │  PC-002    │
│  DISM      │     │  DISM      │
└────────────┘     └────────────┘
```

---

## Quick Start

### One-command installer

On a Linux host with `sudo`, run:

```bash
curl -fsSL https://raw.githubusercontent.com/csufpsudocromis/Bretter-IMG/main/install.sh | bash
```

The installer clones or updates this GitHub repo, installs backend and frontend
dependencies, creates local runtime config/certificates, and starts the API and
web console.

To create the first admin during install:

```bash
curl -fsSL https://raw.githubusercontent.com/csufpsudocromis/Bretter-IMG/main/install.sh | \
  BRETTER_ADMIN_USER=admin BRETTER_ADMIN_PASSWORD='change-this-password' bash
```

By default, the installer disables automatic Samba user provisioning so the
console can start without privileged share setup. Run
`sudo ./scripts/setup_samba_images_share.sh` from the installed repo when you are
ready to configure the image share, then set `SAMBA_AUTO_PROVISION_USERS=true`
in `server/.env` and restart the backend.

### 1. Start the Server Manually

```bash
cd server
pip install -r requirements.txt

# Create first admin user
python ../scripts/create_admin.py admin yourpassword

# Start server
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 2. Start the Frontend

```bash
cd frontend
npm install
npm run dev
# Open http://localhost:3000
```

### 3. Configure the Image SMB Share

Run this once on the Linux host that serves `images-store/`:

```bash
sudo ./scripts/setup_samba_images_share.sh
```

The script creates an authenticated `BretterIMGImages` Samba share, a `bretter-img`
access group, and limited sudo rules so the web server user can add Samba users
when admins create accounts in the console.

### 4. Or use Docker Compose

```bash
SECRET_KEY=your-secret-here docker-compose up -d
# Console: http://localhost:3000
# API:     http://localhost:8000/docs
```

---

## Deploy the Agent to Windows Machines

### Step 1: Build the agent EXE (run on Windows or via cross-compile)

```powershell
cd agent
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --noconsole agent.py
# Creates: dist\agent.exe
```

### Step 2: Get an agent token from the console

1. Log into the web console
2. Navigate to a machine → copy its ID
3. Call: `POST /api/auth/agent-token/{machine_id}` (via Swagger at `/docs`)
4. Copy the returned token

### Step 3: Install the agent

```cmd
# On the target Windows machine (as Administrator):
install.bat
# Enter server URL and agent token when prompted
# Agent installs as "BretterIMGAgent" Windows service
```

The agent will appear in the console within 10 seconds.

---

## Bare-Metal Imaging (WinPE)

For machines with no OS, use the WinPE bootable ISO:

```powershell
# On a Windows machine with ADK installed:
.\scripts\build_winpe.ps1 `
  -AgentExePath ".\agent\dist\agent.exe" `
  -ServerUrl "http://192.168.1.10:8000" `
  -AgentToken "eyJ..." `
  -OutputISO ".\BretterIMG-WinPE.iso"
```

Flash the ISO to a USB drive (Rufus recommended) and boot the target machine from it.
WinPE will auto-start the agent, which will register with the console and await a deploy job.

**Prerequisites:** [Windows ADK](https://docs.microsoft.com/en-us/windows-hardware/get-started/adk-install) + WinPE add-on

### Managed WinPE Boot for Installed Agents

For machines that already have the Bretter-IMG agent installed, the console can stage a local WinPE ramdisk boot environment before capture or deploy:

1. Build or locate `boot.wim` and `boot.sdi` from Windows ADK + WinPE add-on media.
2. Open a machine in the console and upload both files in **WinPE Assets**.
3. Click **Push WinPE** for the target machine.
4. After that job completes, choose **WinPE Offline Capture**.

The agent stores the files under `C:\ProgramData\BretterIMG\winpe`, injects the Bretter-IMG launcher into `boot.wim`, and creates/updates a local BCD entry named `Bretter-IMG WinPE`.

### User-Owned Direct Capture

Admins can create user accounts from **Accounts** in the console. Each account gets
a generated SMB share credential. When a user starts a WinPE direct capture, the
agent maps the image share using that user's SMB credential, so the admin account
is not used to save the WIM.

---

## How Capture Works

1. In the console: select machine(s) → click **Capture**
2. Server creates a `capture` job
3. Agent receives job on next poll
4. Agent runs:
   ```
   dism /Capture-Image /ImageFile:C:\capture.wim /CaptureDir:C:\ /Name:"capture" /Compress:fast
   ```
5. Agent uploads the `.wim` to the server (HTTP multipart)
6. Image appears in the Image Library with SHA-256 checksum
7. Job marked complete

> **Note:** Capturing the live OS drive is supported by DISM but for cleanest results, capture from WinPE.

---

## How Deploy Works

1. In the console: select image → select machines → click **Deploy**
2. Server creates `deploy` jobs for each machine
3. The agent stages a `restore.bat` in the WinPE staging area, then reboots into its pushed WinPE boot entry
4. WinPE loads staged network drivers, maps the **WinPE Assets** image SMB share using the deploying user's SMB credential, and reads the selected WIM directly from that share
5. WinPE auto-runs `restore.bat` which runs:
   ```
   dism /Apply-Image /ImageFile:Z:\selected-image.wim /Index:1 /ApplyDir:C:\ /CheckIntegrity
   ```
6. Machine reboots into freshly imaged Windows

Before deploying, configure the UNC image share and SMB credentials in **WinPE Assets**, run **Push WinPE** on the target, and update installed agents so they receive the current deployment workflow.

---

## API Reference

FastAPI auto-generates interactive docs at `http://your-server:8000/docs`

| Endpoint | Method | Description |
|---|---|---|
| `/api/auth/login` | POST | Get user JWT token |
| `/api/users/` | GET/POST | Admin-only user management and SMB share provisioning |
| `/api/auth/agent-token/{id}` | POST | Issue agent token |
| `/api/machines/register` | POST | Agent registration |
| `/api/machines/` | GET | List all machines |
| `/api/jobs/` | POST | Create imaging job(s) |
| `/api/jobs/pending/{machine_id}` | GET | Agent polls for jobs |
| `/api/jobs/{id}` | PATCH | Agent reports status |
| `/api/images/upload` | POST | Upload WIM from browser |
| `/api/images/agent-upload` | POST | Agent uploads captured WIM |
| `/api/images/{id}/download` | GET | Agent downloads WIM |

---

## Configuration

### Server (`server/.env`)
```env
SECRET_KEY=your-very-long-random-secret
DATABASE_URL=sqlite:///./bretter.db
IMAGE_STORE_PATH=/path/to/images-store
ACCESS_TOKEN_EXPIRE_MINUTES=60
SAMBA_AUTO_PROVISION_USERS=true
SAMBA_GROUP=bretter-img
```

### Agent (`C:\ProgramData\BretterIMG\agent.env`)
```env
BRETTER_SERVER=http://192.168.1.10:8000
BRETTER_AGENT_TOKEN=eyJ...
BRETTER_POLL_INTERVAL=10
```

---

## Project Structure

```
bretter-img/
├── server/          # FastAPI backend
│   ├── app/
│   │   ├── main.py
│   │   ├── core/    # config, security, database
│   │   ├── models/  # SQLAlchemy ORM models
│   │   ├── schemas/ # Pydantic request/response schemas
│   │   └── api/     # route handlers
│   └── Dockerfile
├── agent/           # Windows imaging agent
│   ├── agent.py     # main loop
│   ├── imaging.py   # DISM capture/deploy/wipe
│   ├── transfer.py  # HTTP client
│   ├── sysinfo.py   # system inventory
│   └── install.bat  # Windows service installer
├── frontend/        # React web console
│   └── src/
│       ├── pages/   # Dashboard, Machines, Images, Jobs, Login
│       └── components/
├── scripts/
│   ├── create_admin.py   # First-run admin setup
│   └── build_winpe.ps1   # WinPE ISO builder
├── images-store/    # WIM/IMG file storage
└── docker-compose.yml
```

---

## Security Notes

- Change `SECRET_KEY` in production — never use the default
- Agent tokens are long-lived (1 year) — revoke by re-issuing
- User SMB passwords are generated separately from login passwords and are used for WinPE direct capture share access
- The `/api/images/{id}/download` endpoint currently allows unauthenticated downloads for agent simplicity — add agent token validation in production environments
- Run the server behind a reverse proxy (Nginx/Caddy) with HTTPS in production
- DISM capture requires Administrator privileges on the target machine

---

## License

MIT
