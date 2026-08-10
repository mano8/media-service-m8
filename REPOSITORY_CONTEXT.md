# media-service-m8

## Layer

Service (media storage system).

## Purpose

Handle upload, storage, and lifecycle management of media assets.

## Repository boundaries

- Own the storage layer through the MinIO/filesystem abstraction.
- Do not couple the service to authentication internals.
- Expose a clean API to its consumers.

## Standalone authority

This file, repository documentation, and existing CI are the authoritative local
context. A verified nearest workspace may optionally add launcher-selected
policies and tasks; its absence is a successful standalone condition and does
not make a parent workspace necessary.
