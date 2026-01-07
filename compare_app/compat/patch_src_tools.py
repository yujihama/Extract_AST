from __future__ import annotations

from compare_app.compat.threadlocal_state import ThreadLocalDict


def patch_src_tools_compare_state() -> None:
    """`src.tools.COMPARE_STATE` をスレッドローカルに差し替える。

    目的:
    - InProcessJobQueue（スレッド実行）で複数Runを動かしても混線しない
    - 将来Celery/並列実行へ移行しやすくする

    注意:
    - `src.tools` をimport済みでも差し替え可能
    """
    try:
        import src.tools as tools  # type: ignore
    except Exception:
        return

    # 既にパッチ済みなら何もしない
    if isinstance(getattr(tools, "COMPARE_STATE", None), ThreadLocalDict):
        return

    tools.COMPARE_STATE = ThreadLocalDict()  # type: ignore[attr-defined]

