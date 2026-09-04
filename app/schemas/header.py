import uuid

from pydantic import BaseModel, ConfigDict, Field


class TreeSelection(BaseModel):
    """What the user ticked in the tree.

    Two lists because the tree loads lazily: a folder can be checked before its
    contents have ever been fetched, so the browser can only name the folder
    and the server has to resolve it to documents.
    """

    model_config = ConfigDict(extra="forbid")

    ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    paths: list[str] = Field(default_factory=list, max_length=200)

    @property
    def is_empty(self) -> bool:
        return not self.ids and not self.paths
