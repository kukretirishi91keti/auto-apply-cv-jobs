"""Portal registry with lazy imports to avoid requiring playwright at import time."""

from __future__ import annotations

AUTO_APPLY_PORTALS = {"naukri", "indeed", "foundit", "ziprecruiter"}
SCRAPE_ONLY_PORTALS = {"linkedin", "glassdoor"}

# Map of portal name → module path and class name (lazy-loaded)
_PORTAL_REGISTRY = {
    "naukri": ("src.portals.naukri", "NaukriPortal"),
    "indeed": ("src.portals.indeed", "IndeedPortal"),
    "foundit": ("src.portals.foundit", "FounditPortal"),
    "ziprecruiter": ("src.portals.ziprecruiter", "ZipRecruiterPortal"),
    "linkedin": ("src.portals.linkedin", "LinkedInPortal"),
    "glassdoor": ("src.portals.glassdoor", "GlassdoorPortal"),
}


def get_portal_class(name: str):
    """Lazily import and return the portal class for the given name."""
    if name not in _PORTAL_REGISTRY:
        raise ValueError(f"Unknown portal: {name}")
    module_path, class_name = _PORTAL_REGISTRY[name]
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


class _LazyPortalDict(dict):
    """Dict that lazily imports portal classes on first access."""

    def __getitem__(self, key):
        if key not in _PORTAL_REGISTRY:
            raise KeyError(key)
        return get_portal_class(key)

    def get(self, key, default=None):
        if key in _PORTAL_REGISTRY:
            return get_portal_class(key)
        return default

    def __contains__(self, key):
        return key in _PORTAL_REGISTRY

    def __len__(self):
        return len(_PORTAL_REGISTRY)

    def __iter__(self):
        return iter(_PORTAL_REGISTRY)

    def keys(self):
        return _PORTAL_REGISTRY.keys()

    def items(self):
        for k in _PORTAL_REGISTRY:
            yield k, get_portal_class(k)

    def values(self):
        for k in _PORTAL_REGISTRY:
            yield get_portal_class(k)


ALL_PORTALS = _LazyPortalDict()
