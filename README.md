# weewx-neverin

A [WeeWX](https://weewx.com) extension that uploads your weather station's data to
[Neverin](https://www.neverin.hr), a weather platform that aggregates a large number of weather stations. One
key, one install command, and your station appears on Neverin with its own page, archive and statistics.

- Every archive record is posted to Neverin's station receiver over HTTPS, in the
  [Neverin station protocol](https://www.neverin.hr/en/station-protocol/): metric names and units, the record's own
  time, both pressures, the extra temperature and humidity channels, soil, leaf and particulate sensors, the
  year's rain.
- Nothing else to set up: no FTP, no templates, no cron.

## Requirements

- WeeWX 4.6 or newer (WeeWX 5 recommended), Python 3.7 or newer.
- A Neverin account with the station added under **Profile → My stations**. The station's card shows the
  **sending key** this extension needs. Adding a station is free: [how it works](https://www.neverin.hr/en/connect-station/).

## Install

WeeWX 5:

```sh
weectl extension install https://github.com/neverinhr/weewx-neverin/archive/refs/tags/v1.0.0.zip
```

WeeWX 4:

```sh
wget https://github.com/neverinhr/weewx-neverin/archive/refs/tags/v1.0.0.zip -O weewx-neverin.zip
sudo wee_extension --install weewx-neverin.zip
```

Then put the station's key into `weewx.conf` (`~/weewx-data/weewx.conf` on WeeWX 5, `/etc/weewx/weewx.conf` on
WeeWX 4 and on a package install):

```
[StdRESTful]
    [[Neverin]]
        station_key = 0123456789      # the sending key from the station's card
```

and restart WeeWX:

```sh
sudo systemctl restart weewx
```

The station's card in the Profile changes to "Data arriving" after the first post, which comes with the next
archive record (every 5 minutes unless you changed WeeWX's `archive_interval`).

## Options

All under `[StdRESTful] [[Neverin]]`:

| Option | Default | Meaning |
|---|---|---|
| `station_key` | | The station's sending key: 10 characters, from the card in the Profile. Required. |
| `interval` | `0` | Seconds between posts. `0` posts every archive record at WeeWX's `archive_interval` (300 s unless you changed it in `[StdArchive]`); `900` posts one record in 15 minutes, `3600` one an hour. For a faster station shorten `archive_interval` itself (60 s is the shortest sensible value). |
| `server_url` | `https://ws.neverin.hr/v2` | The receiver. Leave it unless Neverin tells you otherwise. |
| `log_success` | `false` | A syslog line per successful post. |
| `log_failure` | `true` | A syslog line per failed post. |
| `stale` | `10800` | A record older than this many seconds is not posted; the receiver refuses anything more than three hours behind. |
| `timeout`, `max_tries`, `retry_wait`, `retry_login` | `10`, `3`, `5`, `3600` | WeeWX's usual uploader options: the HTTP timeout, the attempts per post, the seconds between them, the pause after a refused key. |

## What is sent

The archive record converted to WeeWX's METRICWX unit system, then renamed to the protocol's names:

| WeeWX | Neverin | Unit |
|---|---|---|
| `dateTime` | `dateutc` | the record's time, UTC |
| `outTemp`, `outHumidity` | `temp`, `rh` | °C, % |
| `barometer`, `pressure` | `baro_rel`, `baro_abs` | hPa (Neverin computes its own sea-level pressure from `baro_abs`) |
| `windSpeed`, `windGust`, `windDir` | `wavg`, `wgust`, `wdir` | m/s (the interval's mean and gust), ° |
| the year's rain (summed from the archive) | `precip_year` | mm, a counter; the receiver makes the amounts |
| `UV`, `radiation` | `uv`, `solar` | index, W/m² |
| `extraTemp1..8`, `extraHumid1..8` | `temp_ch1..8`, `rh_ch1..8` | °C, %; Neverin gives each channel its role when the station is approved |
| `soilTemp1..4`, `soilMoist1..4` | `soil_temp_1..4`, `soil_moist_1..4` | °C, cb |
| `leafWet1..2` | `leaf_wet_1..2` | |
| `pm1_0`, `pm2_5`, `pm10_0` | `pm1`, `pm2p5`, `pm10` | µg/m³ |
| `snowDepth` | `snow_depth` | cm |
| `inTemp`, `inHumidity`, `lightning_strike_count`, `lightning_distance` | `temp_in`, `rh_in`, `lightning_count`, `lightning_dist` | kept with the sample |
| `[Station] station_type`, the versions | `model`, `software` | what the station reports about itself |

Not sent, because Neverin derives them itself: dew point, wind chill, heat index, rain rate, the hour's and the
day's rain. A None or missing observation is left out of the post.

### Why archive records, not LOOP packets

An archive record is the complete sample of its interval: the means, the gust, the rain as a sum. LOOP packets
differ from driver to driver (a Davis emits one every 2 seconds, others every 20 to 60 seconds, some only parts of
the sensors at a time) and carry the console's own rain counters, which would not agree with the archive's. Neverin
builds its data in 5-minute steps, so the archive record is what it needs. A live mode may come later if there is
a call for it.

## Checking and troubleshooting

- `weectl extension list` shows `neverin` with its version. `sudo journalctl -u weewx -f` (or `/var/log/syslog`)
  shows `Neverin: version 1.0.0, every archive record to https://ws.neverin.hr/v2` at startup.
- **"Neverin refused the key (401 …)" or "(403 …)"**: the key is mistyped, or the station was withdrawn, rejected
  or expired in the Profile. The service pauses for an hour after such an answer; fix the key and restart WeeWX.
- **"Neverin refused the sample (400 …)"**: the receiver could not read the record, usually a time more than three
  hours old (a backlog after a long outage; those records are skipped) or more than five minutes ahead (check the
  computer's clock).
- **Nothing arrives, no error**: WeeWX writes archive records only when the station reports; check that WeeWX
  itself is getting data (`weectl debug` or the standard report), and that `station_key` sits under
  `[StdRESTful] [[Neverin]]`.
- Upgrading: install the new version over the old one; your `station_key` stays.
- Uninstall: `weectl extension uninstall neverin` (WeeWX 4: `wee_extension --uninstall neverin`) and a restart.

Questions and problems: [GitHub issues](https://github.com/neverinhr/weewx-neverin/issues) or info@neverin.hr.

## Development

```sh
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest
PYTHONPATH=bin venv/bin/python bin/user/neverin.py      # one body, from a sample record
```

The tests need no WeeWX installation beyond the `weewx` package from PyPI: they cover the body, the units, the
year's rain query, the receiver's answers and the installer. An end-to-end run uses WeeWX's simulator driver
against a receiver of your own (`server_url`).

## Licence

GPLv3, as WeeWX. Copyright 2026 Neverin (Alen Šterpin).
