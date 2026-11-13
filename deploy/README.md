# Running the watcher on a headless host

The bot is one headless Chromium session with a persistent Facebook profile, so
any always-on Linux box will do it. A Hetzner Cloud CX22 (2 vCPU, 4 GB,
~€4/month) is comfortably more than it needs; the whole poll is two page loads
plus an occasional notification probe.

`fb-watcher.service` in this folder is the unit. This page is the order to do
things in. Commands assume the repo lives at `/home/watcher/fb-iphone-watcher`;
change that in the unit if you put it elsewhere.

## 1. User and checkout

```bash
sudo adduser --disabled-password --gecos "" watcher
sudo apt update && sudo apt install -y python3-venv git
sudo -u watcher git clone https://github.com/Numba1ne/fb-iphone-watcher.git \
    /home/watcher/fb-iphone-watcher
sudo -u watcher git -C /home/watcher/fb-iphone-watcher checkout freshness-gate
```

## 2. Python dependencies and Chromium

```bash
cd /home/watcher/fb-iphone-watcher
sudo -u watcher python3 -m venv .venv
sudo -u watcher .venv/bin/pip install -r requirements.txt
# install-deps shells out to apt for Chromium's shared libraries, so it is root.
sudo .venv/bin/python -m playwright install --with-deps chromium
```

## 3. Configuration

```bash
cp config.example.yaml config.yaml      # search location, price band, filters
cp .env.example .env                    # Telegram token and/or Discord webhook
```

Both files are gitignored, so they never arrive with a clone — write them here
or `scp` them over. The Marketplace location is the numeric `city_slug` plus
`latitude`/`longitude` in `config.yaml`; it does not depend on where the server
is, and the browser's locale/timezone are pinned to en-GB / Europe-London in
`watcher/browser.py`.

## 4. The Facebook session

The watcher reads a logged-in profile from `data/browser-profile`. Logging in
from a datacentre IP is the one step likely to trigger a checkpoint, so prefer
copying a profile that already works:

```bash
# On the machine where `python main.py --login` has already succeeded, with its
# watcher stopped (Chromium allows one session per profile):
rsync -a data/browser-profile/ \
    watcher@HOST:/home/watcher/fb-iphone-watcher/data/browser-profile/
sudo chown -R watcher:watcher /home/watcher/fb-iphone-watcher/data
```

The IP change can still prompt a check the first time. If you would rather log
in on the server, it needs a display to show you: `apt install x11vnc xvfb`,
run `xvfb-run -s "-screen 0 1400x1000x24" x11vnc -localhost -forever`, tunnel
port 5900 (`ssh -L 5900:127.0.0.1:5900 watcher@HOST`), then run
`.venv/bin/python main.py --login` from that shell and drive it over VNC.

## 5. Start it

```bash
sudo cp deploy/fb-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fb-watcher
journalctl -u fb-watcher -f      # a HEARTBEAT line per poll
```

The first poll after any start is a silent warm-up: it records what is already
on the search page and announces nothing.

## 6. The dashboard

It binds to `127.0.0.1` only, so reach it through a tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 watcher@HOST
# then open http://127.0.0.1:8765
```

## Updating

```bash
sudo -u watcher git -C /home/watcher/fb-iphone-watcher pull
sudo systemctl restart fb-watcher
```

## What can still go wrong

- **Marketplace may refuse a datacentre IP.** If the search comes back empty or
  the profile lands on a login wall, check `journalctl` for the session-expired
  alert; a residential proxy in the launch args is the usual fix.
- **Alerts are Telegram and Discord only** — the phone receiving them needs one
  of those apps installed.
- **Account risk is real.** Scraping Marketplace breaks Facebook's terms, and a
  flagged account can be restricted; use an account you would not mind losing.
