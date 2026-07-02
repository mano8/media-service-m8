# media-service-m8

## Layer
Service (media storage system)

---

## Purpose
Handles upload, storage, lifecycle of media assets.

---

## Rules
- Owns storage layer (MinIO / filesystem abstraction)
- No coupling to auth internals
- Must expose clean API only
---

## Authority
All rules come from /.Codex/policy.index.json (type: python)

