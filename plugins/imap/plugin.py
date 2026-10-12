"""Plugin entry point: thin re-export of the real manifest module.

The loader imports this file under a synthetic module name
(``quest_plugin_imap``); the real code lives in the normal
``plugins.imap.*`` package path so imports inside the plugin resolve to
a single module instance.
"""

from plugins.imap.manifest import get_plugin

__all__ = ["get_plugin"]
