# Roomba Cloud

[![Tests](https://github.com/carbon198/roomba-cloud/actions/workflows/test.yaml/badge.svg?branch=main&event=push)](https://github.com/carbon198/roomba-cloud/actions/workflows/test.yaml)
[![Latest release](https://img.shields.io/github/v/release/carbon198/roomba-cloud)](https://github.com/carbon198/roomba-cloud/releases/latest)
[![Publish release](https://github.com/carbon198/roomba-cloud/actions/workflows/release.yaml/badge.svg?event=release)](https://github.com/carbon198/roomba-cloud/actions/workflows/release.yaml)
[![Container image](https://img.shields.io/badge/GHCR-container_image-blue?logo=docker&logoColor=white)](https://github.com/carbon198/roomba-cloud/pkgs/container/roomba-cloud)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Roomba Cloud is a local replacement for the cloud services used
by iRobot V4 robots. It lets a compatible robot connect to your own server,
keeping its telemetry and maps on your network and making them available to
[Roomba Local for Home Assistant](https://github.com/carbon198/ha-roomba-local).
No custom robot firmware is required.

This is an independent project, not affiliated with iRobot.

## Supported models

| Model | Tested firmware | Status |
|---|---|---|
| Roomba 505/507 | `p25-505+9.3.6+I3.8.149` | Physically tested |

Other V4 models have not been verified.

## Requirements

- A domain name you control, with a publicly trusted TLS certificate for it. 
  The emulator includes ACME/Let's Encrypt support
- A server running Docker with TCP ports **443** (HTTPS) and **8883** (MQTT) 
  reachable from the robot and 443 reachable from Home Assitant
- A Bluetooth-capable computer near the robot for provisioning, with Python
  and Bluetooth access. **This can be a different computer from the server.**

**Keep the emulator on a trusted, firewalled LAN. Do not expose it to the
Internet.**

## Installation

### 1. Prepare DNS 

Configure DNS so that `roomba.example.com` resolves to your Docker server's LAN
address from the robot's network. Keep the default published ports, 443 and
8883, available. This A record can be local, it doesn't need to be on public DNS.

### 2. Choose one installation method:

- [Docker Compose](#docker-compose): use the supplied configuration to 
  create both containers and their storage.
- [Manual container setup](#manual-container-setup):
  configure the containers through TrueNAS, Unraid, Portainer, or another
  container manager.
- [Native Python installation](#native-python-installation-without-docker):
  run directly on a Linux host without Docker.

### Docker Compose

Download the Compose file and environment template from the latest stable release:

```sh
mkdir roomba-cloud
cd roomba-cloud
curl -fL https://github.com/carbon198/roomba-cloud/releases/latest/download/compose.yaml -o compose.yaml
curl -fL https://github.com/carbon198/roomba-cloud/releases/latest/download/.env.example -o .env.example
cp .env.example .env
mkdir -p certs
```

#### 1. Configuration

Edit `.env` and set:

```dotenv
PUBLIC_URL=https://roomba.example.com
```

Leave the port and storage settings at their defaults.

#### 2. Choose how to obtain a certificate

**Option A: Built-in Let's Encrypt DNS validation**

In `.env`, leave `TLS_CERT` and `TLS_KEY` empty and set:

```dotenv
TLS_CERT=
TLS_KEY=
ACME_EMAIL=you@example.com
ACME_AGREE_TOS=true
ACME_STAGING=false
```

Use your own email address. Set `ACME_AGREE_TOS=true` only after accepting the
[Let's Encrypt subscriber agreement](https://letsencrypt.org/repository/).
You will need to add a TXT to your DNS provider record after starting the containers below.

**Option B: Supply an existing certificate**

Copy the full certificate chain and matching unencrypted private key into
`certs/fullchain.pem` and `certs/privkey.pem`.

Set these values in `.env`:

```dotenv
TLS_CERT=/certs/fullchain.pem
TLS_KEY=/certs/privkey.pem
```

The containers run as UID/GID **10001**, so give that user access:

```sh
sudo chown 10001:10001 certs certs/fullchain.pem certs/privkey.pem
sudo chmod 700 certs
sudo chmod 600 certs/fullchain.pem certs/privkey.pem
```

User-supplied certificate and ACME are mutually exclusive.

#### 3. Download and start the server

```sh
docker compose pull
docker compose up -d
docker compose logs -f https
```

If using built-in Let's Encrypt, wait for an `acme_dns_challenge` event:

```json
{"event":"acme_dns_challenge","name":"_acme-challenge.roomba.example.com","type":"TXT","value":"<challenge value>"}
```

Add the exact TXT value at the public DNS name shown in the log. Leave the
containers running: the server waits for DNS propagation, then requests the
certificate automatically. It allows up to 30 minutes for propagation. If the
attempt times out, follow the logs for the next attempt and its current value.

##### Automating DNS updates with another app (optional)

The HTTPS service also writes `/data/dns-challenges/challenge.json`:

```json
{"name":"_acme-challenge.roomba.example.com","type":"TXT","value":"<challenge value>"}
```

The separate Compose `dns-challenges` volume contains only these public challenge
details.

Have your DNS updater read `/challenges/challenge.json`, add the TXT value
through your DNS provider. When the cert is renewed, `challenge.json` will be deleted.

#### 4. Get the customer token

The customer token is your login credential and is generated automatically on first startup

```sh
docker compose exec https cat /data/customer-token.txt
```

Paste the key into the web login page. Save it somewhere; the same key is also
used by Home Assistant.

### Manual container setup

| Setting | Value |
|---|---|
| Image repository | `ghcr.io/carbon198/roomba-cloud` |
| Image tag | Your chosen release tag, or `latest` |
| Published TCP ports (host → container) | `443` → `8443` and `8883` → `8883` |

With **host networking**, omit port mappings and set `HTTPS_PORT=443` and
`MQTT_PORT=8883`.

#### Environment and certificates

Set `PUBLIC_URL=https://roomba.example.com`, using your
own hostname. Configure DNS as described in the Compose instructions.

For built-in Let's Encrypt, set these additional variables:

| Variable | Value |
|---|---|
| `ACME_EMAIL` | Your email address |
| `ACME_AGREE_TOS` | `true`, after accepting the Let's Encrypt terms |

Leave `TLS_CERT` and `TLS_KEY` unset in this mode. Alternatively, mount supplied
certificates as described below and set `TLS_CERT=/certs/fullchain.pem` and
`TLS_KEY=/certs/privkey.pem`, leaving ACME options unset.
Other settings can remain at their defaults.

#### Storage

| Storage | Mount inside container | Access |
|---|---|---|
| Roomba Data/Maps | `/data` | Read/write |
| DNS challenge directory (optional for sharing with an updater) | `/data/dns-challenges` | Read/write |
| Supplied certificate directory (not needed for built-in ACME) | `/certs` | Read-only |

The private `/data` directory must be owned by UID/GID **10001** with mode
**0700**. Keep it on local storage supporting Unix sockets.

#### Start and sign in

Start the container. For built-in ACME, watch its logs and
complete the TXT challenge as described above, or have your DNS updater read
the shared challenge directory. MQTT waits until the certificate is available.

Open your configured HTTPS address. In your container manager's console run:

```sh
cat /data/customer-token.txt
```

Use that customer key to sign in, then continue to Bluetooth provisioning.

### Native Python installation (without Docker)

Download the source first:

```sh
git clone https://github.com/carbon198/roomba-cloud.git
cd roomba-cloud
```

From that repository directory:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p tools
cc -O2 -Wall -Wextra -Werror native/mqtt_tls_bridge.c -o tools/mqtt_tls_bridge -lssl -lcrypto
```

Run these in **two terminals**, both in the repository directory. These 
examples use `sudo` to bind HTTPS port 443; both processes must run as the 
same user and share the same data directory.

```sh
# Terminal 1
sudo .venv/bin/python -m roomba_cloud mqtt \
  --public-url https://roomba.example.com --https-port 443 --data-dir ./data \
  --acme-email you@example.com --acme-agree-tos
```

```sh
# Terminal 2
sudo .venv/bin/python -m roomba_cloud https \
  --public-url https://roomba.example.com --https-port 443 --data-dir ./data \
  --acme-email you@example.com --acme-agree-tos
```

Retrieve the key with `sudo cat ./data/customer-token.txt`. These commands run
in the foreground; arrange service supervision yourself if needed.

## Provision the robot over Bluetooth

Start the emulator and verify its HTTPS page works before provisioning.
Provisioning changes the robot's Wi-Fi and cloud settings and will disconnect
the robot from the cloud. It won't break Matter integrations though.
There is no need to factory reset the robot. The emulator will pull your existing
maps from the robot.

**Provisioning does not have to run on the emulator server.** Clone or download
this repository on any Bluetooth-capable computer, move near the
robot, and run the tool there.

### Install the provisioning tool

Download the source on the Bluetooth computer, if you do not already have it:

```sh
git clone https://github.com/carbon198/roomba-cloud.git
cd roomba-cloud
```

From that repository directory:

**Linux / macOS**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-provisioning.txt
.venv/bin/python -m provisioning.ble_provision --ssid 'YOUR_WIFI_SSID' \
  --domain roomba.example.com --country US --timezone America/New_York
```

**Windows (PowerShell)**

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-provisioning.txt
.\.venv\Scripts\python.exe -m provisioning.ble_provision --ssid "YOUR_WIFI_SSID" --domain roomba.example.com --country US --timezone America/New_York
```

Replace the Wi-Fi name, country, and timezone with your own values.

### Follow the prompts

1. Keep the robot nearby and ready for Wi-Fi/Bluetooth setup. Select a Bluetooth
   adapter if prompted, then select your robot from the scan results.
2. Enter the Wi-Fi password at the hidden prompt.
3. Follow the authorization prompt. If requested, press the robot's **Power**
   button once after it beeps. An already authorized robot may proceed without
   another button press.
4. Wait while settings are written and the robot joins Wi-Fi. The tool displays
   a generated robot password once; retain it privately if needed. This is
   **not** the customer key used to sign in.
5. Open the dashboard link, sign in with your customer key, and confirm the robot
   appears with an online MQTT connection.

If you encounter issues, add `--debug` to the provisioning command for additional diagnostics

## Using the web interface

Open your configured HTTPS address and enter the customer key. The browser
remembers it across refreshes and restarts until you **Sign out** or clear the
site's stored data. Use this only on a trusted device.

The dashboard provides:

- **Robot status:** robot ID, reported model/SKU, IP address, MQTT connection,
  battery, mission phase, firmware, and last-seen time. It refreshes automatically.
- **Maps and rooms:** open a robot's room editor and select a saved map to view it.
- **Map editing:** rename rooms, split a room, merge adjacent rooms, or delete
  an available map. These actions change the robot's saved map, not just the
  displayed image. Map changes cannot be reverted.

Editing and deletion require the robot to be idle with no mission. Controls
are locked during running or paused missions, or when mission state is unknown.
After an edit, wait for the robot's updated map before making another change.
Maps and room details appear as the robot reports them; missing data is not
necessarily a setup failure.

For cleaning controls and day-to-day automation, install
[Roomba Local for Home Assistant](https://github.com/carbon198/ha-roomba-local)
and follow its setup instructions. Use the emulator's HTTPS address, customer
key, and the robot ID shown on the dashboard.

## Reconnecting to Roomba's Cloud

If you want to reconnect your Roomba to the manufacturer's cloud, simply follow the
Change Wi-Fi process in the Roomba Home app. Click your robot -> Robot Settings ->
Change Wi-Fi.

## FAQ

**Q:** Will this wipe my Robot? **A:** No, the provisioning is the same as the Roomba Home app's Change Wi-Fi feature. It does not wipe the robot
**Q:** Can I use my existing maps? **A:** Yes please do, the emulator will pull them from the robot automatically
**Q:** Should I block the Roomba's internet? **A:** I do, and you probably should. I can't guarantee they won't block this provisioning process in the future.
**Q:** Can this brick the robot? **A:** No not to my knowledge, we aren't flashing the firmware. Using the map editor always entails some risk you might lose or corrupt your maps, but that's the extent of the risk.
