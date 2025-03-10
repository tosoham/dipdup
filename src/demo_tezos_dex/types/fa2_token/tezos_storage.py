# generated by DipDup 8.1.1

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict


class Key(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    address: str
    nat: str


class LedgerItem(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    key: Key
    value: str


class Key1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    owner: str
    operator: str
    token_id: str


class Operator(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    key: Key1
    value: dict[str, Any]


class TokenMetadata(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    token_id: str
    token_info: dict[str, str]


class Fa2TokenStorage(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    administrator: str
    all_tokens: str
    ledger: list[LedgerItem]
    metadata: dict[str, str]
    operators: list[Operator]
    paused: bool
    token_metadata: dict[str, TokenMetadata]
