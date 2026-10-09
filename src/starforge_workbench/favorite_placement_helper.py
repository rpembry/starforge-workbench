"""Optional placement bridge; never launches, focuses or closes applications."""
import json
import sys
from gi.repository import Gio, GLib

try:
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    # Address GNOME Shell directly: another bus client cannot impersonate it.
    bus.call_sync('org.gnome.Shell', '/org/starforge/Workbench/FavoritePlacement',
                  'org.starforge.Workbench.FavoritePlacement', 'PlaceConfigured',
                  GLib.Variant('(s)', (sys.argv[1],)), None,
                  Gio.DBusCallFlags.NONE, 2000, None)
    print(json.dumps({'placement': 'requested'}))
except Exception:
    print(json.dumps({'placement': 'unavailable'}))
