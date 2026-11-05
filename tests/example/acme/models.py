from dataclasses import dataclass


@dataclass
class Stock:
    sku: str
    count: int
