"""genericMud — accessible, cross-platform, self-voicing MUD client.

``__version__`` is the single source of truth for the version: pyproject reads it
dynamically (``[tool.hatch.version]``), and the self-updater reads it directly so
detecting a newer release never depends on ``importlib.metadata`` resolving in the
frozen build (which silently returns nothing when the dist-info isn't bundled).
"""

__version__ = "0.6.7"
