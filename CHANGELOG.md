# Changelog

## 1.0.0 (2026-11-11)

- The first release: every archive record posted to Neverin's receiver in the Neverin station protocol, version 2
  (metric names and units, the record's own time, both pressures, the extra channels, soil, leaf and particulate
  sensors, the year's rain as a counter summed from the archive); `interval` slows the posts; the receiver's
  refusals read as a bad key (an hour's pause) or a refused sample (no retry); a record older than the receiver's
  three-hour window is not posted.
