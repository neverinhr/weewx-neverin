# The installer of weewx-neverin, for weectl extension install (WeeWX 5) and wee_extension --install (WeeWX 4).
# Copyright 2026 Neverin (Alen Šterpin)
# Distributed under the terms of the GNU General Public License v3 (GPLv3); see LICENSE.

from io import StringIO

import configobj
from weecfg.extension import ExtensionInstaller

VERSION = "1.0.0"

# The stanza merged into weewx.conf: the owner replaces station_key with the station's key from the Profile's card
CONFIG = """
[StdRESTful]
    [[Neverin]]
        # The station's sending key, from the station's card in the Profile on neverin.hr (My stations)
        station_key = replace_me
        # Seconds between posts: 0 sends every archive record (WeeWX's archive_interval), 900 one in 15 minutes
        interval = 0
"""


def loader():
	return NeverinInstaller()


class NeverinInstaller(ExtensionInstaller):
	def __init__(self):
		super().__init__(
			version=VERSION,
			name="neverin",
			description="Upload weather data to Neverin (neverin.hr).",
			author="Neverin",
			author_email="info@neverin.hr",
			restful_services="user.neverin.Neverin",
			config=configobj.ConfigObj(StringIO(CONFIG)),
			files=[("bin/user", ["bin/user/neverin.py"])],
		)
