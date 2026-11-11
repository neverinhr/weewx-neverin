"""
weewx-neverin without a WeeWX engine: the body of a post (the names, the units, what is left out), the year's rain
query, the receiver's answers and refusals, the service's options and thread, the installer.
"""

import io
import json
import queue
import urllib.error
import urllib.request
from datetime import datetime, timezone

import configobj
import pytest
import weewx
import weewx.restx

import neverin
from neverin import Neverin, NeverinThread, payload

KEY = "0a1b2c3d4e"		# made up: 10 lower-case hex characters, as every Neverin key
TS = int(datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc).timestamp())

US_RECORD = {
	"dateTime": TS, "usUnits": weewx.US, "interval": 5,
	"outTemp": 55.6, "outHumidity": 97.0, "barometer": 30.36, "pressure": 29.39,
	"windSpeed": 3.1, "windGust": 7.0, "windDir": 83.0, "rain": 0.01, "rainRate": 0.02, "dewpoint": 54.9,
	"UV": 1.2, "radiation": 120.0, "yearRain": 25.059, "dayRain": 0.098,
	"extraTemp1": 60.8, "extraHumid2": 55.0, "soilTemp1": 50.0, "soilMoist1": 12.0, "leafWet1": 3.0,
	"pm2_5": 7.5, "snowDepth": 0.5, "inTemp": 71.2, "inHumidity": 61.0, "lightning_strike_count": 3, "lightning_distance": 6.2,
	"windchill": None,
}


# =============================================================================
# THE BODY
# =============================================================================

def test_the_body_has_the_protocols_names_and_metric_units():
	body = payload(US_RECORD, software="weewx-5 neverin-1.0.0", model="Vantage")
	assert body["dateutc"] == "2026-11-11 12:00:00"
	assert (body["software"], body["model"]) == ("weewx-5 neverin-1.0.0", "Vantage")
	assert body["temp"] == round((55.6 - 32) * 5 / 9, 2) and body["rh"] == 97.0
	assert body["baro_rel"] == round(30.36 * 33.86389, 2) and body["baro_abs"] == round(29.39 * 33.86389, 2)
	assert body["wavg"] == round(3.1 * 0.44704, 2) and body["wgust"] == round(7.0 * 0.44704, 2) and body["wdir"] == 83.0
	assert body["precip_year"] == round(25.059 * 25.4, 2)
	assert (body["uv"], body["solar"]) == (1.2, 120.0)
	assert body["temp_ch1"] == 16.0 and body["rh_ch2"] == 55.0
	assert body["soil_temp_1"] == 10.0 and body["soil_moist_1"] == 12.0 and body["leaf_wet_1"] == 3.0
	assert body["pm2p5"] == 7.5
	assert body["snow_depth"] == round(0.5 * 25.4 / 10, 1)		# inches to mm, then to cm
	assert body["temp_in"] == 21.78 and body["rh_in"] == 61.0
	assert (body["lightning_count"], body["lightning_dist"]) == (3, round(6.2 * 1.609344, 1))


def test_what_neverin_derives_itself_and_what_is_missing_stay_out():
	body = payload(US_RECORD)
	for name in ("rain", "rainRate", "dewpoint", "dayRain", "windchill", "interval", "usUnits", "dateTime", "software", "model"):
		assert name not in body
	assert "wspeed" not in body and "precip_day" not in body and "precip" not in body


def test_a_metric_record_converts_nothing_and_a_metric_one_in_km_h_converts_the_wind():
	metric = {"dateTime": TS, "usUnits": weewx.METRICWX, "outTemp": 13.1, "windSpeed": 1.4, "barometer": 1013.2}
	assert payload(metric) == {"dateutc": "2026-11-11 12:00:00", "temp": 13.1, "baro_rel": 1013.2, "wavg": 1.4}
	metric_cm = {"dateTime": TS, "usUnits": weewx.METRIC, "windSpeed": 36.0, "rain": 0.1}		# km/h and cm
	assert payload(metric_cm)["wavg"] == 10.0


# =============================================================================
# THE YEAR'S RAIN
# =============================================================================

class FakeManager:
	table_name = "archive"

	def __init__(self, row):
		self.row = row
		self.queries = []

	def getSql(self, sql, params):
		self.queries.append((sql, params))
		return self.row


def test_the_years_rain_is_summed_from_the_first_of_january_in_the_records_units():
	manager = FakeManager((2.5, weewx.US, weewx.US))
	record = {"dateTime": TS, "usUnits": weewx.US}
	assert NeverinThread.year_rain(record, manager) == 2.5
	sql, (start, end) = manager.queries[0]
	assert sql.startswith("SELECT SUM(rain), MIN(usUnits), MAX(usUnits) FROM archive WHERE dateTime > ? AND dateTime <= ?")
	assert end == TS and datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M") == "2026-01-01 00:00"
	assert NeverinThread.year_rain(record, FakeManager((None, None, None))) == 0.0		# a dry year so far
	assert NeverinThread.year_rain(record, FakeManager((2.5, weewx.US, weewx.METRIC))) is None		# mixed units: no counter


def test_get_record_adds_the_counter_when_the_archive_is_at_hand():
	thread = NeverinThread(queue.Queue(), KEY, manager_dict=None)
	manager = FakeManager((1.0, weewx.US, weewx.US))
	full = thread.get_record({"dateTime": TS, "usUnits": weewx.US, "outTemp": 50.0}, manager)
	assert full["yearRain"] == 1.0 and "dayRain" in full		# WeeWX's own sums come too
	assert "yearRain" not in thread.get_record({"dateTime": TS, "usUnits": weewx.US}, None)		# no archive at hand


# =============================================================================
# THE REQUEST AND THE ANSWERS
# =============================================================================

class FakeResponse(io.BytesIO):
	def __init__(self, body, code=200):
		super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
		self.code = code


def test_the_url_and_the_body_of_a_post():
	thread = NeverinThread(queue.Queue(), KEY, server_url="https://ws.neverin.hr/v2/", station_type="Simulator")
	assert thread.format_url({}) == f"https://ws.neverin.hr/v2/{KEY}"
	body, content_type = thread.get_post_body({"dateTime": TS, "usUnits": weewx.METRICWX, "outTemp": 10.0})
	assert content_type == "application/json"
	assert json.loads(body) == {"dateutc": "2026-11-11 12:00:00", "temp": 10.0, "model": "Simulator", "software": thread.softwaretype}
	assert thread.softwaretype.startswith("weewx-") and thread.softwaretype.endswith(f"neverin-{neverin.VERSION}")


def test_a_key_that_does_not_look_like_one_is_warned_about(caplog):
	NeverinThread(queue.Queue(), "replace_me")
	assert "does not look like a Neverin sending key" in caplog.text


def test_the_receivers_answers():
	thread = NeverinThread(queue.Queue(), KEY)
	thread.check_response(FakeResponse({"meta": {}, "data": {"station_id": 1, "stored": True}}))
	thread.check_response(FakeResponse({"data": {"stored": False}}))		# within the floor: nothing to do
	with pytest.raises(weewx.restx.FailedPost):
		thread.check_response(FakeResponse({"error": {"code": "NO_VALUES", "message": "nothing"}}))
	with pytest.raises(weewx.restx.FailedPost):
		thread.check_response(FakeResponse(b"<html>not json</html>"))


def test_the_receivers_refusals(monkeypatch):
	thread = NeverinThread(queue.Queue(), KEY)
	request = urllib.request.Request(thread.format_url({}))

	def refused(code, body):
		def fake_urlopen(req, data=None, timeout=None):
			raise urllib.error.HTTPError(req.full_url, code, "refused", {}, FakeResponse(body, code))
		monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

	refused(403, {"error": {"code": "STATION_REJECTED", "message": "the request was rejected"}})
	with pytest.raises(weewx.restx.BadLogin, match="STATION_REJECTED"):
		thread.post_request(request, "{}")
	refused(401, {"error": {"code": "STATION_KEY_INVALID", "message": "no station has this key"}})
	with pytest.raises(weewx.restx.BadLogin, match="STATION_KEY_INVALID"):
		thread.post_request(request, "{}")
	refused(400, {"error": {"code": "INVALID_PARAMETER", "message": "the time is outside the window", "field": "dateutc"}})
	with pytest.raises(weewx.restx.FailedPost, match="INVALID_PARAMETER"):
		thread.post_request(request, "{}")
	refused(503, b"unavailable")
	with pytest.raises(urllib.error.HTTPError):		# retried by post_with_retries
		thread.post_request(request, "{}")
	monkeypatch.setattr(urllib.request, "urlopen", lambda req, data=None, timeout=None: FakeResponse({"data": {"stored": True}}))
	assert thread.post_request(request, "{}").code == 200


# =============================================================================
# THE SERVICE
# =============================================================================

class FakeEngine:
	def __init__(self):
		self.bound = {}

	def bind(self, event_type, callback):
		self.bound[event_type] = callback


def config(stanza):
	return configobj.ConfigObj(io.StringIO(f"""
[Station]
    station_type = Simulator
[StdRESTful]
    log_success = True
    [[Neverin]]
{stanza}
"""))


def test_the_service_posts_every_archive_record_by_default():
	engine = FakeEngine()
	service = Neverin(engine, config(f"        station_key = {KEY}"))
	try:
		assert list(engine.bound) == [weewx.NEW_ARCHIVE_RECORD]
		thread = service.archive_thread
		assert (thread.station_key, thread.server_url, thread.station_type) == (KEY, neverin.DEFAULT_URL, "Simulator")
		assert thread.post_interval is None and thread.stale == neverin.RECEIVER_WINDOW
		assert thread.log_success is False and thread.log_failure is True		# ours, under the global True
		engine.bound[weewx.NEW_ARCHIVE_RECORD](type("Event", (), {"record": {"dateTime": TS}})())
		assert service.archive_queue.get(timeout=1) == {"dateTime": TS}
	finally:
		service.shutDown()


def test_the_services_options(caplog):
	import logging
	caplog.set_level(logging.INFO, logger="neverin")
	engine = FakeEngine()
	service = Neverin(engine, config(f"        station_key = {KEY}\n        interval = 900\n        server_url = http://127.0.0.1:8070/v2/\n        log_success = true\n        colour = blue"))
	try:
		thread = service.archive_thread
		assert (thread.post_interval, thread.server_url, thread.log_success) == (900, "http://127.0.0.1:8070/v2", True)
		assert "the option 'colour' is unknown" in caplog.text
		assert "an archive record every 900 s" in caplog.text
	finally:
		service.shutDown()


def test_the_service_stays_off_without_a_key():
	engine = FakeEngine()
	service = Neverin(engine, config("        interval = 0"))
	assert engine.bound == {} and not hasattr(service, "archive_thread")


# =============================================================================
# THE INSTALLER
# =============================================================================

def test_the_installer():
	import importlib.util
	from pathlib import Path
	spec = importlib.util.spec_from_file_location("install", Path(__file__).resolve().parent.parent / "install.py")
	install = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(install)
	installer = install.loader()
	assert installer["name"] == "neverin" and installer["version"] == neverin.VERSION == install.VERSION
	assert installer["restful_services"] == "user.neverin.Neverin"
	assert installer["config"]["StdRESTful"]["Neverin"]["station_key"] == "replace_me"
	assert installer["files"] == [("bin/user", ["bin/user/neverin.py"])]
