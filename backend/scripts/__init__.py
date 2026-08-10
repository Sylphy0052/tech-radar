"""backend 直下の保守用スクリプト置き場。

`tests/` と同様、`uv run python -m scripts.<module>` の形で実行できるよう
パッケージ化している（`backend` を cwd にした際に `tests` パッケージを
import できるようにするため）。
"""

from __future__ import annotations
