# generated by DipDup 8.0.0b2

from __future__ import annotations

from pydantic import BaseModel
from pydantic import ConfigDict


class PoolCreatedPayload(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    token0: str
    token1: str
    fee: int
    tickSpacing: int
    pool: str
