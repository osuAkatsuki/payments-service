import httpx

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    ...

http = httpx.AsyncClient()
