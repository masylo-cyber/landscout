import importlib
import pkgutil

import landscout


def test_all_modules_import():
    for m in pkgutil.walk_packages(landscout.__path__, "landscout."):
        importlib.import_module(m.name)
