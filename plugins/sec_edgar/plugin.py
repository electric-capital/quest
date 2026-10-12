"""Plugin entry point: thin re-export of the real manifest module.

The loader imports this file under a synthetic module name
(``quest_plugin_sec_edgar``); the real code lives in the normal
``plugins.sec_edgar.*`` package path so imports inside the plugin resolve
to a single module instance.
"""

from plugins.sec_edgar.manifest import get_plugin

__all__ = ["get_plugin"]
