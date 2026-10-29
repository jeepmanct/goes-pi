# GOES-19 on a Raspberry Pi

A home receiver for **GOES-19 (GOES-East) HRIT**: full-disk satellite images, mesoscale scenes and the NWS
EMWIN weather bulletins, received straight from the satellite at 1694.1 MHz with a small dish and an RTL-SDR,
then turned into a phone-friendly web panel with time-lapses, a weather page, alerts and a morning email.

It is built on [goestools](https://github.com/pietern/goestools) (the demodulator and decoder). Everything in this
repo is the layer on top: the control panel, image products, weather cards, alerts and email.

> NOAA's old APT weather satellites are retired. GOES HRIT is the practical free satellite signal for a home
> station today, and it needs no internet connection to receive.

## What you get

- **Live signal tools**: Viterbi error and packet charts, an Aim page with sun/moon position, an audio aiming
  tone and Geiger-style packet clicks, a gain sweep that finds the best tuner gain, outage history with likely causes.
- **Views**: whole-disk globe and regional crops, surface temperature, radar over clouds, night fog, cloud tops,
  storm view, and time-lapses (GIF/MP4).
- **Weather page**: local conditions, forecast, tides, winter and tropical cards, sunset-glow outlook, all from
  the EMWIN feed and public NWS/NOAA data.
- **Alerts**: signal lost, receiver restarts, disk or temperature problems, NWS warnings, Skywarn items, by phone push
  (ntfy) and email. Technical alerts go to one primary address; weather warnings go to everyone.
- **Email**: a daily morning briefing with pictures and forecast text, a weekly time-lapse.
- **Sharing**: optional per-person read-only picture links through Tailscale Funnel.

## Equipment

| Part | Notes |
|---|---|
| **Raspberry Pi 4 or 5**, 4 GB+ | Built and run on a Pi 4 (64-bit Raspberry Pi OS / Debian 13). A Pi 3 can decode but is tight. |
| **Storage** | A 64 GB+ SD card works; the images add up (about 15 GB/week of raw scans before pruning). A USB SSD or large SD card is better. |
| **RTL-SDR with a bias tee** | An RTL-SDR Blog V3/V4 or a Nooelec NESDR SmarTee style dongle (R820T tuner). The bias tee powers the LNA. |
| **Nooelec SAWbird+ GOES** | Low-noise amplifier with a 1694 MHz SAW filter. Sits at the dish feed and is powered over the coax by the dongle's bias tee. |
| **Dish and L-band feed** | A dish with a feed made for 1.7 GHz (or a purpose-made GOES dish kit). Home GOES stations are commonly built around a dish of about 1 m or more; bigger gives more margin. Check the goestools and community guides for a build that suits you. |
| **Coax and adapters** | Short, good-quality coax from the SAWbird+ to the dongle (SMA/N adapters as needed). Keep the LNA end as close to the feed as you can. |
| **Pi power supply, case and cooling** | The decoder uses about one CPU core continuously; a case with a fan or heatsink keeps it under throttle temperatures. |
| **A clear view of the satellite** | GOES-East sits over the equator at 75° W, so you need an unobstructed line of sight to that point in the sky. Which way and how high depends on where you live; the Aim page shows the exact elevation and azimuth for the location in `site.json`. Trees and buildings in the path cost signal. |

Optional: a second always-on device or a cloud account for the dead-man-switch ping (healthchecks.io or similar).

## Accounts and apps

| Thing | Why | Cost |
|---|---|---|
| **[Tailscale](https://tailscale.com)** (on the Pi and your phone/laptop) | Reach the panel securely from anywhere (`tailscale serve`), and optionally publish read-only picture links (`tailscale funnel`). | Free tier |
| **[ntfy](https://ntfy.sh)** app on your phone | Free push notifications. You pick a hard-to-guess topic name and subscribe to it in the app. | Free |
| **An SMTP account** | Sends the morning briefing and alert emails. Tested with iCloud Mail and an app-specific password; any SMTP server with STARTTLS works. | Free |
| A GitHub account | Only if you want to keep your own copy of the code. | Free |

No paid APIs and no AI service are used. Weather text comes from the satellite's own EMWIN feed; the tide and
tropical cards use public NOAA data.

## Setup

These steps assume Raspberry Pi OS / Debian on a Pi, a user named `pi`, and this repo cloned to `~/goes`
(paths inside the code and service files use `/home/pi/goes`).

### 1. Packages

```bash
sudo apt update
sudo apt install -y git cmake build-essential libopencv-dev zlib1g-dev libusb-1.0-0-dev \
    librtlsdr-dev libairspy-dev rtl-sdr ffmpeg python3-numpy python3-pil
```

Python 3.11 or newer is required (the panel uses `tomllib`). `ffmpeg` builds the MP4 time-lapses.

### 2. Build goestools

```bash
mkdir -p ~/src && cd ~/src
git clone https://github.com/pietern/goestools
cd goestools && mkdir -p build && cd build
cmake ../ -DCMAKE_INSTALL_PREFIX=/usr/local
make -j2
sudo make install
```

This provides `goesrecv` and `goesproc` in `/usr/local/bin`. Plug in the dongle and check `rtl_test -t` sees it.
If the dongle is not accessible without root, install the RTL-SDR udev rules (the `rtl-sdr` package does).

### 3. Get the code

```bash
cd ~ && git clone https://github.com/jeepmanct/goes-pi goes
cd ~/goes
```

### 4. Say where you are

```bash
cp site.example.json site.json
nano site.json          # {"name": "Your Town, ST", "lat": 40.0, "lon": -75.0}
```

`site.json` is git-ignored. It drives the sun/moon geometry, the sunset outlook, the home marker on the maps and the
local weather. The weather cards and bulletin filters in `webui/local.py` and `webui/skywarn.py` also name a nearby
airport station, NWS zone and forecast office (`KBDR`, `CTZ009`, `OKX` in the shipped code); change those to yours.
`keep-offices.txt` lists the NWS offices whose text bulletins are kept.

### 5. Receiver settings

`goesrecv.conf` and `goesproc.conf` are ready to use for an RTL-SDR with the SAWbird+ (HRIT, 1694.1 MHz, 2.4 MS/s,
bias tee on). The one setting you will tune is `gain` in `goesrecv.conf`; the panel's Aim page and **Gain sweep**
(Signal tab) help with that. Images and bulletins are written under `~/goes/data/`.

### 6. Start the control panel

```bash
sudo cp goes-webui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now goes-webui
```

The panel listens on port 80, starts `goesrecv` and `goesproc` itself and restarts them if they die. Do **not**
enable the separate `goesrecv.service` / `goesproc.service` as well: they are for running without the panel and would
fight it for the SDR.

On first start the panel creates `webui/password.txt` (a random password; read it with `cat`). Sign in with user
`pi` and that password. Five wrong attempts lock an address out for 15 minutes.

### 7. Reach it from your phone with Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg 80      # https://<pi-name>.<tailnet>.ts.net, tailnet only
```

Install Tailscale on your phone and sign into the same account. Requests that arrive through `tailscale serve` from
your own devices skip the password; direct LAN access still needs it. Add the page to your Home Screen for an app-like
icon. If iCloud Private Relay is on, turn it off for the Tailscale address.

### 8. Scheduled jobs

```bash
crontab -e
```
```cron
*/10 * * * * /home/pi/goes/prune-data.sh
*/5  * * * * nice -n 10 /usr/bin/python3 -I /home/pi/goes/views.py >> /tmp/goes-views.log 2>&1
*/10 * * * * /usr/bin/python3 -I /home/pi/goes/digest.py --if-due >> /tmp/goes-digest.log 2>&1
*/30 * * * * /usr/bin/python3 -I /home/pi/goes/storm.py >> /tmp/goes-storm.log 2>&1
*/15 * * * * nice -n 10 /usr/bin/python3 -I /home/pi/goes/insights.py >> /tmp/goes-insights.log 2>&1
```

`prune-data.sh` keeps the data folder in check (raw scans after 180 days, EMWIN after 3, and a size backstop;
edit the variables at the top). `views.py` builds the rendered pictures, `digest.py` the morning email,
`storm.py` the tropical storm view and `insights.py` the Insights tab.

### 9. Email (optional)

```bash
cp mail.conf.example mail.conf
nano mail.conf
install -m 600 /dev/null mail-password.txt && nano mail-password.txt   # the SMTP / app password, nothing else
```

For iCloud, create an **app-specific password** at appleid.apple.com. List recipients with the primary address
first. Use the panel's **Mail** tab to add or remove recipients and send a test.

### 10. Phone pushes with ntfy (optional)

Install the ntfy app, then open the panel's **Notifications** tab, press **Make one** to generate a topic (or type your
own), save it, subscribe to the same topic in the app and press **Send test push**. Choose which kinds of alert reach
your phone on the same page. Treat the topic name like a password: anyone who knows it can read your pushes.

### 11. Public picture links (optional)

Run `sudo tailscale funnel --bg --https=8443 8090`, set `public_url` in `mail.conf`, then create one link per person
on the **Mail** tab. Links show pictures only, nothing from the dish or the receiver, and can be cancelled one by one.

### 12. Backups (optional)

`backup_state.py` archives the small files that are not in git (passwords, mail settings, histories) into
`data/backup/` weekly, and the panel runs it for you. Put `data/` on a USB drive so it survives an SD failure.
A drive plugged into the Pi will not survive theft, fire or a power surge, so copy it elsewhere now and then.

## Day to day

- **Aim the dish** from the Aim tab: watch Viterbi errors fall and packets per second climb, or turn on the tone and the
  clicks and listen. Once locked you should see about 55 packets per second with no Reed-Solomon errors.
- **Check the gain** with the Gain sweep on the Signal tab (about 20 minutes, restarts the receiver at each step).
- **Weather**, **Insights**, **Views** and **Charts** are the pages you will use most. The **More** menu holds Skywarn,
  Aim, Signal, Pi health, Receiver settings, Notifications, Mail and the file browser.
- After editing panel code (`webui/*.py`), restart it: `sudo systemctl restart goes-webui` (the receiver drops for about a minute).
  Static files under `webui/static/` take effect on a page reload.

## Repo layout

| Path | What it is |
|---|---|
| `webui/app.py` | The control panel (standard-library Python HTTP server) and its API |
| `webui/static/` | The single-page front end |
| `webui/watch.py` | Watchdog, signal history, alerts and the background checks |
| `webui/local.py`, `products.py`, `skywarn.py`, `tides.py`, `winter.py`, `tropical.py`, `sunset.py` | Weather cards and outlooks |
| `webui/gainsweep.py` | Tuner gain sweep |
| `views.py`, `surface.py`, `storm.py`, `monthly.py` | Image products and time-lapses |
| `digest.py`, `insights.py` | Morning email, Insights tab |
| `backup_state.py`, `prune-data.sh` | Backups and retention |
| `goesrecv.conf`, `goesproc.conf` | goestools settings |
| `*.service` | systemd units |
| `site.example.json`, `mail.conf.example` | Templates for your private settings |

Tests for the weather parsers: `python3 -m unittest discover -s webui/tests`.

## Privacy and safety

- Your location (`site.json`), passwords, mail settings, share links and signal history are **git-ignored** and stay on the Pi.
  Do not commit them.
- The panel is intended to be reached through Tailscale, not exposed to the open internet. Plain HTTP on the LAN
  is password-protected but unencrypted.
- The SAWbird+ is powered through the coax by the bias tee. Do not connect anything else that does not expect DC on that line.

## Credits

[goestools](https://github.com/pietern/goestools) by Pieter Noordhuis does the actual receiving and decoding.
Weather data comes from NOAA, the National Weather Service and the satellite's EMWIN feed; map outlines from
[Natural Earth](https://www.naturalearthdata.com/).
