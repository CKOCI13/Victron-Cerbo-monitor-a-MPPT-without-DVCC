"""Stand-in for Victron's ``vedbus`` module (part of velib_python), used only by the tests.

The real ``VeDbusService`` publishes values on the Linux D-Bus, which isn't available on a
Windows or Mac PC. This copy keeps the same interface and the same rules, so the driver's D-Bus
code can be tested anywhere:

* paths must be added before ``register()`` is called
* ``service[path] = value`` sets a value (what the driver does)
* ``SetValue(path, value)`` is what the Cerbo screen, VRM or MQTT do. It returns velib's codes:
  0 = accepted, 1 = path not writable, 2 = refused by the driver's callback.
"""


class VeDbusService:
    def __init__(self, servicename, bus=None, register=None):
        self.name, self.paths, self.registered = servicename, {}, register is not False

    def register(self):
        self.registered = True

    def add_path(self, path, value, description="", writeable=False, onchangecallback=None,
                 gettextcallback=None, valuetype=None, itemtype=None):
        assert not self.registered, 'paths must be added before register()'
        assert path not in self.paths, 'duplicate path ' + path
        self.paths[path] = dict(value=value, writeable=writeable, cb=onchangecallback)

    def __getitem__(self, path):
        return self.paths[path]['value']

    def __setitem__(self, path, value):
        self.paths[path]['value'] = value

    def SetValue(self, path, value):
        """An outside writer (GX screen, VRM, MQTT) changing a value; same return codes as velib."""
        item = self.paths[path]
        if not item['writeable']:
            return 1
        if value == item['value']:
            return 0
        if item['cb'] is None or item['cb'](path, value):
            item['value'] = value
            return 0
        return 2
