"""DropTracker knowledgebase (KB) package.

Mines Discord content (ticket transcripts, suggestion/bug forums, general
chat) plus repo docs into kb_documents / kb_chunks (see
db/models/knowledgebase.py), then serves hybrid retrieval (MariaDB FULLTEXT +
local fastembed vectors) and answer synthesis via the Claude Code CLI
(subscription auth — no per-token API cost).

Modules (import submodules directly; this __init__ deliberately re-exports
nothing so a missing optional dependency in one module never breaks another):
- chunker    : message/markdown -> chunk text windows
- embedder   : optional local embeddings (fastembed, KB_EMBEDDINGS flag)
- miner      : source ingesters (tickets/docs sync; forums/chat via Discord REST)
- retriever  : hybrid keyword+semantic search with reciprocal-rank fusion
- answerer   : claude -p subprocess synthesis over retrieved context
"""
