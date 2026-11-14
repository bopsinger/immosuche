from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Listing:
    source: str            # z.B. "Flatfox", "Homegate"
    key: str               # eindeutige Kennung innerhalb der Quelle
    url: str
    title: str = ""
    price: float | None = None
    rooms: float | None = None
    area: float | None = None
    zipcode: str = ""
    city: str = ""
    street: str = ""
    image: str = ""
    available: str = ""
    extra_ids: set[str] = field(default_factory=set)  # z.B. SMG-ID für Portalübergreifende Duplikate

    @property
    def uid(self) -> str:
        return f"{self.source.lower()}:{self.key}"

    def fingerprint(self) -> str | None:
        """Grober Schlüssel, um dasselbe Objekt auf mehreren Portalen zu erkennen."""
        if self.price and self.rooms and self.zipcode:
            return f"{int(round(self.price))}|{self.rooms:g}|{self.zipcode}"
        return None
