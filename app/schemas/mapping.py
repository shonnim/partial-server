from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class MappingEntry(BaseModel):
    """One publication row pasted from the source open-data system.

    The source keys are mapped onto the project vocabulary here, at the
    boundary, so everything after this point speaks the documents table's
    column names. Two listing formats exist and both are accepted:

    - 재정간행물 — Korean keys: 유형명, 파일명, 게시물ID, 첨부파일ID, 첨부순번.
    - 재정보고서 및 문서 — camelCase keys: allDtaClsNm, dpFileNm, odtId,
      atchFileId, atchFileSeq, with the file extension split off into fileExt.
      odtId plays the 게시물ID role and lands in ``source_document_id``.
    """

    model_config = ConfigDict(populate_by_name=True)

    document_type: str = Field(validation_alias=AliasChoices("유형명", "allDtaClsNm"))
    name: str = Field(validation_alias=AliasChoices("파일명", "dpFileNm"))
    source_document_id: str = Field(validation_alias=AliasChoices("게시물ID", "odtId"))
    atch_file_id: str = Field(validation_alias=AliasChoices("첨부파일ID", "atchFileId"))
    atch_file_seq: int = Field(validation_alias=AliasChoices("첨부순번", "atchFileSeq"))

    @model_validator(mode="before")
    @classmethod
    def _join_file_ext(cls, data: object) -> object:
        # The camelCase format sends "dpFileNm" without its extension and the
        # extension alone in "fileExt". Glue them back together so the name
        # compares and displays like a 파일명 — in particular a ".zip" must
        # stay visible for the archive-to-folder match to trigger.
        if isinstance(data, dict):
            name, ext = data.get("dpFileNm"), data.get("fileExt")
            if isinstance(name, str) and isinstance(ext, str) and ext.strip():
                ext = ext.strip().lstrip(".")
                if not name.casefold().endswith(f".{ext.casefold()}"):
                    data = {**data, "dpFileNm": f"{name}.{ext}"}
        return data

    @field_validator("source_document_id", mode="before")
    @classmethod
    def _coerce_id(cls, value: object) -> object:
        # 재정간행물 sends 게시물ID as a number; the column is text.
        return str(value) if isinstance(value, int) else value
