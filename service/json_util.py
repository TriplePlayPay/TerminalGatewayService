from dataclasses import fields, is_dataclass, asdict
from json import JSONEncoder
from typing import TypeVar, Type, Any
from uuid import UUID

_T = TypeVar("_T")


def ignore_properties(cls: Type[_T], dict_: Any) -> _T:
    """omits extra fields like @JsonIgnoreProperties(ignoreUnknown = true)"""
    if isinstance(dict_, cls):
        return dict_  # noqa
    class_fields = {f.name for f in fields(cls)}
    filtered = {k: v for k, v in dict_.items() if k in class_fields}
    return cls(**filtered)


class EnhancedJSONEncoder(JSONEncoder):
    def default(self, o):
        if is_dataclass(o):
            return asdict(o)
        if isinstance(o, UUID):
            return str(o)
        return super().default(o)
