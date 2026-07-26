from engine.providers.base import ProspectProvider, RawProspect
from engine.providers.csv_provider import CsvProvider
from engine.providers.gosom import GosomProvider
from engine.providers.registry import RegistryProvider


def get_provider(name: str) -> ProspectProvider:
    providers = {
        "gosom": GosomProvider,
        "csv": CsvProvider,
        "registry": RegistryProvider,
    }
    if name not in providers:
        raise ValueError(f"Unknown PROSPECT_PROVIDER '{name}'. Options: {list(providers)}")
    return providers[name]()
