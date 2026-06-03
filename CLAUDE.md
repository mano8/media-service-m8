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
- No cross-service DB access

---

## Authority
All rules come from:
- /.claude/context/python.md
- /.claude/architecture.md