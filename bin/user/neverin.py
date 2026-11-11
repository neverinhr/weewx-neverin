#!/usr/bin/env python3
# Copyright 2026 Neverin (Alen Šterpin)
# Distributed under the terms of the GNU General Public License v3 (GPLv3); see LICENSE.
"""
Upload a WeeWX station's data to Neverin, https://www.neverin.hr.

The extension is a WeeWX RESTful service (weewx.restx). Every archive record goes to the Neverin station
receiver as one HTTPS POST in the Neverin station protocol, version 2
(https://www.neverin.hr/en/station-protocol/): a JSON body with metric names and units (°C, hPa, m/s, mm, W/m²,
µg/m³), the record's own time, both pressures, the extra temperature and humidity channels, the soil, leaf and
particulate sensors, and the year's rain as a counter the receiver turns into rain amounts. The archive record is
the complete, aggregated sample of the interval (the means, the gust, the rain), which is why it is the one sent;
LOOP packets are not.

Configuration, in weewx.conf::

    [StdRESTful]
        [[Neverin]]
            station_key = replace_me    # the station's sending key, from the Profile's card on neverin.hr
            # interval = 0              # seconds between posts; 0 = every archive record (WeeWX's archive_interval)
            # server_url = https://ws.neverin.hr/v2

The key is issued when the station is added in the Profile ("My stations"); the data show on the card within
minutes of the first post. Requires WeeWX 4.6 or newer on Python 3.7 or newer.
"""

import inspect
import json
import logging
import queue
import sys
import time
import urllib.error
from datetime import datetime, timezone

import weewx
import weewx.manager
import weewx.restx
import weewx.units
from weeutil.weeutil import to_int, version_compare

VERSION = "1.0.0"
REQUIRED_WEEWX = "4.6.0"

if version_compare(weewx.__version__, REQUIRED_WEEWX) < 0:
	raise weewx.UnsupportedFeature(f"weewx-neverin {VERSION} needs WeeWX {REQUIRED_WEEWX} or newer, not {weewx.__version__}")

log = logging.getLogger(__name__)

PROTOCOL = "Neverin"
DEFAULT_URL = "https://ws.neverin.hr/v2"
RECEIVER_WINDOW = 3 * 3600		# the receiver refuses a time more than 3 hours behind its arrival: older records are not posted
KEY_LENGTH = 10					# a Neverin sending key: 10 lower-case hexadecimal characters
HEX = set("0123456789abcdef")

# The year's rain, summed from the archive by get_record, converts like every rain amount
weewx.units.obs_group_dict.setdefault("yearRain", "group_rain")

# (WeeWX observation, the protocol's name, decimals). The record is converted to METRICWX first, so the units are
# the protocol's: °C, hPa (mbar), m/s, mm, W/m², µg/m³, percent, degrees, km. The snow depth is handled apart
# (payload): WeeWX keeps it in mm, the protocol in cm.
FIELDS = (
	("outTemp", "temp", 2),
	("outHumidity", "rh", 1),
	("barometer", "baro_rel", 2),			# reduced to sea level, for a station without an absolute pressure
	("pressure", "baro_abs", 2),			# the station pressure: Neverin computes its own relative from it
	("windSpeed", "wavg", 2),				# an archive record's windSpeed is the interval's mean
	("windGust", "wgust", 2),
	("windDir", "wdir", 0),
	("UV", "uv", 2),
	("radiation", "solar", 1),
	("yearRain", "precip_year", 2),			# the counter; the receiver makes the rain amounts from its differences
	("soilTemp1", "soil_temp_1", 2), ("soilTemp2", "soil_temp_2", 2), ("soilTemp3", "soil_temp_3", 2), ("soilTemp4", "soil_temp_4", 2),
	("soilMoist1", "soil_moist_1", 1), ("soilMoist2", "soil_moist_2", 1), ("soilMoist3", "soil_moist_3", 1), ("soilMoist4", "soil_moist_4", 1),
	("leafWet1", "leaf_wet_1", 1), ("leafWet2", "leaf_wet_2", 1),
	("pm1_0", "pm1", 1), ("pm2_5", "pm2p5", 1), ("pm10_0", "pm10", 1),
	("inTemp", "temp_in", 2), ("inHumidity", "rh_in", 1),
	("lightning_strike_count", "lightning_count", 0), ("lightning_distance", "lightning_dist", 1),
) + tuple((f"extraTemp{n}", f"temp_ch{n}", 2) for n in range(1, 9)) \
  + tuple((f"extraHumid{n}", f"rh_ch{n}", 1) for n in range(1, 9))


def payload(record, software=None, model=None):
	"""
	The JSON body of one post: the record in the protocol's names and units. An observation that is None or missing
	is left out.
	"""
	metric = weewx.units.to_METRICWX(record)
	body = {"dateutc": datetime.fromtimestamp(metric["dateTime"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
	if software:
		body["software"] = software
	if model:
		body["model"] = model
	for observation, name, decimals in FIELDS:
		value = metric.get(observation)
		if value is not None:
			body[name] = round(float(value), decimals)
	if metric.get("snowDepth") is not None:
		body["snow_depth"] = round(float(metric["snowDepth"]) / 10.0, 1)
	return body


def refusal(response):
	"""The receiver's reason from its error envelope ({"error": {"code", "message"}}), or the raw body, for a log line."""
	try:
		raw = response.read()
	except Exception:
		return ""
	try:
		error = json.loads(raw).get("error") or {}
		return " ".join(str(part) for part in (error.get("code"), error.get("message")) if part)
	except (ValueError, AttributeError):
		return raw[:200].decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)[:200]


class NeverinThread(weewx.restx.RESTThread):
	"""One post per archive record from the queue: the JSON body to <server_url>/<station_key>, the answer checked."""

	def __init__(self, q, station_key, server_url=DEFAULT_URL, station_type=None, manager_dict=None,
			post_interval=None, max_backlog=sys.maxsize, stale=RECEIVER_WINDOW, log_success=False, log_failure=True,
			timeout=10, max_tries=3, retry_wait=5, retry_login=3600, retry_ssl=3600, skip_upload=False):
		super().__init__(
			q, protocol_name=PROTOCOL, manager_dict=manager_dict, post_interval=post_interval,
			max_backlog=max_backlog, stale=stale, log_success=log_success, log_failure=log_failure, timeout=timeout,
			max_tries=max_tries, retry_wait=retry_wait, retry_login=retry_login, retry_ssl=retry_ssl,
			softwaretype=f"weewx-{weewx.__version__} neverin-{VERSION}", skip_upload=skip_upload,
		)
		self.station_key = str(station_key).strip()
		self.server_url = str(server_url).strip().rstrip("/")
		self.station_type = station_type
		if len(self.station_key) != KEY_LENGTH or not set(self.station_key) <= HEX:
			log.warning("Neverin: the station_key does not look like a Neverin sending key (%d lower-case hex characters); "
				"copy it from the station's card in the Profile", KEY_LENGTH)

	def get_record(self, record, dbmanager):
		"""WeeWX's record (with the day's rain summed) plus the year's rain, the counter the receiver reads."""
		full = super().get_record(record, dbmanager)
		if dbmanager is not None and full.get("yearRain") is None:
			full["yearRain"] = self.year_rain(full, dbmanager)
		return full

	@staticmethod
	def year_rain(record, dbmanager):
		"""
		The rain from 1 January (local time) to the record's time, summed from the archive table, in the record's
		unit system. None when the archive mixes unit systems, which WeeWX's own uploaders refuse too.
		"""
		ts = record["dateTime"]
		local = time.localtime(ts)
		start = time.mktime((local.tm_year, 1, 1, 0, 0, 0, 0, 0, -1))
		row = dbmanager.getSql(
			f"SELECT SUM(rain), MIN(usUnits), MAX(usUnits) FROM {dbmanager.table_name} WHERE dateTime > ? AND dateTime <= ?",
			(start, ts),
		)
		if not row or row[0] is None:
			return 0.0
		if row[1] != row[2] or row[1] != record["usUnits"]:
			log.error("Neverin: the archive mixes unit systems (%s, %s) with the record's (%s); no rain counter sent", row[1], row[2], record["usUnits"])
			return None
		return float(row[0])

	def format_url(self, _record):
		return f"{self.server_url}/{self.station_key}"

	def get_post_body(self, record):
		body = payload(record, software=self.softwaretype, model=self.station_type)
		return json.dumps(body), "application/json"

	def post_request(self, request, data=None):
		"""
		The receiver's refusals (https://www.neverin.hr/en/station-protocol/): 401 and 403 mean the key is wrong,
		withdrawn, rejected or expired (BadLogin: WeeWX pauses this service for retry_login seconds instead of
		retrying every few seconds); another 4xx is a sample the receiver cannot take (FailedPost: logged, not
		retried); a 5xx and a network error are retried by post_with_retries.
		"""
		try:
			return super().post_request(request, data)
		except urllib.error.HTTPError as error:
			reason = refusal(error)
			if error.code in (401, 403):
				raise weewx.restx.BadLogin(f"Neverin refused the key ({error.code} {reason}); check station_key in weewx.conf")
			if 400 <= error.code < 500:
				raise weewx.restx.FailedPost(f"Neverin refused the sample ({error.code} {reason})")
			raise

	def check_response(self, response):
		"""A 2xx answer in the receiver's envelope: stored, or within its 2 s floor ("stored": false, nothing lost)."""
		raw = response.read()
		try:
			answer = json.loads(raw)
		except ValueError:
			raise weewx.restx.FailedPost(f"not the receiver's answer: {raw[:200]!r}")
		if "error" in answer:
			error = answer["error"] or {}
			raise weewx.restx.FailedPost(f"Neverin refused the sample ({error.get('code')} {error.get('message')})")
		data = answer.get("data") or {}
		if data.get("stored") is False:
			log.debug("Neverin: the sample came within the receiver's floor and was not stored")


class Neverin(weewx.restx.StdRESTful):
	"""The service: every archive record on the queue of one thread."""

	def __init__(self, engine, config_dict):
		super().__init__(engine, config_dict)
		site = weewx.restx.get_site_dict(config_dict, "Neverin", "station_key")
		if site is None:
			return		# get_site_dict has logged why (no stanza, or no station_key)
		stanza = config_dict["StdRESTful"]["Neverin"]
		if "log_success" not in stanza:
			site["log_success"] = False		# WeeWX's default logs a line per post; one every few minutes is too much for syslog
		interval = to_int(site.pop("interval", 0) or 0)
		known = set(inspect.signature(NeverinThread.__init__).parameters) - {"self", "q", "station_type", "manager_dict"}
		for option in [name for name in site if name not in known]:
			log.warning("Neverin: the option %r is unknown and ignored", option)
			site.pop(option)
		try:
			site["manager_dict"] = weewx.manager.get_manager_dict_from_config(config_dict, "wx_binding")
		except weewx.UnknownBinding:
			pass		# no archive to sum the rain from: the posts go without the counter
		station_type = config_dict.get("Station", {}).get("station_type")

		self.archive_queue = queue.Queue()
		self.archive_thread = NeverinThread(self.archive_queue, station_type=station_type, post_interval=interval or None, **site)
		self.archive_thread.start()
		self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)
		log.info("Neverin: version %s, %s to %s", VERSION,
			"every archive record" if not interval else f"an archive record every {interval} s", site.get("server_url", DEFAULT_URL))

	def new_archive_record(self, event):
		self.archive_queue.put(event.record)


if __name__ == "__main__":
	# A quick look at one body, no WeeWX engine: PYTHONPATH=bin python3 bin/user/neverin.py
	sample = {"dateTime": int(time.time()), "usUnits": weewx.US, "outTemp": 55.6, "outHumidity": 97, "barometer": 30.36,
		"pressure": 29.39, "windSpeed": 3.1, "windGust": 7.0, "windDir": 83, "rain": 0.01, "UV": 1.2, "radiation": 120.0,
		"yearRain": 25.059, "extraTemp1": 60.8, "snowDepth": 0.5}
	print(json.dumps(payload(sample, software=f"weewx-{weewx.__version__} neverin-{VERSION}", model="Vantage"), indent=2))
