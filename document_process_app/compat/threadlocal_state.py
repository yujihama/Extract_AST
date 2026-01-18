from __future__ import annotations

import threading
from collections.abc import MutableMapping
from typing import Any, Iterator, Mapping, Optional


class ThreadLocalDict(MutableMapping[str, Any]):
    """dict互換のスレッドローカルProxy。

    `src.tools.COMPARE_STATE` のような“モジュールグローバル辞書”を、
    実行スレッドごとに分離する目的で使用する。
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def _d(self) -> dict[str, Any]:
        d = getattr(self._local, "data", None)
        if d is None:
            d = {}
            self._local.data = d
        return d

    # --- MutableMapping ---
    def __getitem__(self, key: str) -> Any:
        return self._d()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._d()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._d()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._d())

    def __len__(self) -> int:
        return len(self._d())

    # --- dict-like helpers used in existing code ---
    def clear(self) -> None:  # type: ignore[override]
        self._d().clear()

    def update(self, other: Mapping[str, Any] | None = None, **kwargs: Any) -> None:  # type: ignore[override]
        if other is not None:
            self._d().update(dict(other))
        if kwargs:
            self._d().update(kwargs)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return self._d().get(key, default)

    def pop(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return self._d().pop(key, default)

    def setdefault(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return self._d().setdefault(key, default)

    def to_dict(self) -> dict[str, Any]:
        """現在スレッドの実体dictをコピーして返す（デバッグ用）。"""
        return dict(self._d())

