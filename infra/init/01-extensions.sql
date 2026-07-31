-- pgvector 拡張を有効化する。
-- このスクリプトは初回起動時 (データディレクトリが空のとき) にのみ実行される。
CREATE EXTENSION IF NOT EXISTS vector;
