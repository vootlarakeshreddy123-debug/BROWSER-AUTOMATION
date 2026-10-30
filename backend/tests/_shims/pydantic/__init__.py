"""TEST-ONLY minimal pydantic-v2 look-alike (see tests/_shims/README.md)."""
from __future__ import annotations
import enum, sys, typing
from typing import Any, Union, get_args, get_origin

_MISSING = object()


class ValidationError(ValueError):
    pass


class _FieldInfo:
    def __init__(self, default=_MISSING, default_factory=None, ge=None, le=None):
        self.default, self.default_factory, self.ge, self.le = default, default_factory, ge, le


def Field(default=_MISSING, *, default_factory=None, ge=None, le=None, **kw):
    return _FieldInfo(default, default_factory, ge, le)


def field_validator(*names, **_):
    def deco(fn):
        f = fn.__func__ if isinstance(fn, classmethod) else fn
        f._fv_names = names
        return f
    return deco


def model_validator(*, mode="after", **_):
    def deco(fn):
        fn._mv = mode
        return fn
    return deco


def _coerce(v, hint):
    if v is None:
        return None
    origin = get_origin(hint)
    if origin is Union or (origin is not None and "UnionType" in str(origin)):
        last = None
        for a in [a for a in get_args(hint) if a is not type(None)]:
            try:
                return _coerce(v, a)
            except Exception as e:  # noqa: BLE001
                last = e
        raise last or ValidationError("no union member matched")
    if origin is typing.Literal:
        if v not in get_args(hint):
            raise ValidationError(f"{v!r} not in {get_args(hint)}")
        return v
    if origin in (list, typing.List):
        (inner,) = get_args(hint) or (Any,)
        if not isinstance(v, list):
            raise ValidationError("expected list")
        return [_coerce(i, inner) for i in v]
    if isinstance(hint, type):
        if issubclass(hint, enum.Enum):
            return hint(v)
        if issubclass(hint, BaseModel):
            if isinstance(v, hint):
                return v
            if isinstance(v, dict):
                return hint(**v)
            raise ValidationError("expected object")
        if hint in (int, float):
            return hint(v)
        if hint is str and not isinstance(v, str):
            raise ValidationError("expected str")
    return v


class BaseModel:
    @classmethod
    def _hints(cls):
        hints = {}
        for klass in reversed(cls.__mro__):
            if klass is object or klass is BaseModel:
                continue
            mod = sys.modules[klass.__module__]
            hints.update(typing.get_type_hints(klass, vars(mod), dict(vars(klass))))
        return hints

    def __init__(self, **data):
        cls = type(self)
        for name, hint in cls._hints().items():
            if name.startswith("_"):
                continue
            default = getattr(cls, name, _MISSING)
            if isinstance(default, _FieldInfo):
                info = default
                if name in data:
                    val = data[name]
                elif info.default_factory is not None:
                    val = info.default_factory()
                elif info.default is not _MISSING:
                    val = info.default
                else:
                    raise ValidationError(f"{name} required")
                if val is not None and ((info.ge is not None and val < info.ge) or (info.le is not None and val > info.le)):
                    raise ValidationError(f"{name} out of range")
            elif name in data:
                val = data[name]
            elif default is not _MISSING:
                val = default
            else:
                raise ValidationError(f"{name} required")
            object.__setattr__(self, name, _coerce(val, hint))
        for klass in reversed(cls.__mro__):
            for attr in list(vars(klass).values()):
                for n in getattr(attr, "_fv_names", ()) or ():
                    object.__setattr__(self, n, attr(cls, getattr(self, n)))
        for klass in reversed(cls.__mro__):
            for attr in list(vars(klass).values()):
                if getattr(attr, "_mv", None) == "after":
                    attr(self)

    @classmethod
    def model_validate(cls, obj):
        return obj if isinstance(obj, cls) else cls(**obj)

    def model_dump(self, mode="python", **_):
        def dump(v):
            if isinstance(v, BaseModel):
                return v.model_dump(mode=mode)
            if isinstance(v, enum.Enum):
                return v.value if mode == "json" else v
            if isinstance(v, list):
                return [dump(i) for i in v]
            if isinstance(v, dict):
                return {k: dump(i) for k, i in v.items()}
            return v
        return {n: dump(getattr(self, n)) for n in type(self)._hints() if not n.startswith("_")}

    def model_copy(self, update=None, **_):
        data = {n: getattr(self, n) for n in type(self)._hints() if not n.startswith("_")}
        data.update(update or {})
        return type(self)(**data)

    def __repr__(self):
        return f"{type(self).__name__}({self.model_dump()})"
