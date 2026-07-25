from engine.providers.base import ProspectProvider, RawProspect
from engine.providers.csv_provider import CsvProvider
from engine.providers.gosom import GosomProvider
from engine.providers.osm import OsmProvider
from engine.providers.places import PlacesProvider
from engine.providers.registry import RegistryProvider

PROVIDERS = {
    "registry": RegistryProvider,
    "osm": OsmProvider,
    "places": PlacesProvider,
    "csv": CsvProvider,
    "gosom": GosomProvider,
}


def get_provider(name: str) -> ProspectProvider:
    if name not in PROVIDERS:
        raise ValueError(f"Unknown PROSPECT_PROVIDER '{name}'. Options: {list(PROVIDERS)}")
    return PROVIDERS[name]()


def provider_availability() -> dict[str, str]:
    """provider -> '' if usable, else a human reason (for the admin UI)."""
    from pathlib import Path

    from engine.config import get_settings

    settings = get_settings()
    notes = {"registry": "", "osm": "", "csv": ""}
    notes["places"] = ("" if settings.places_api_key
                       else "PLACES_API_KEY not set")
    notes["gosom"] = ("" if Path(settings.gosom_binary).exists()
                      else f"binary not found at {settings.gosom_binary}")
    return notes
