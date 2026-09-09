# Respaldo y simulacro de restauracion

2026-09-09, commit 424b731

```
$ COMPOSE_PROJECT_NAME=poc uv run python scripts/backup.py --out var/backups
[OK  ] postgres: 4801 KiB
[OK  ] qdrant: 0.9 MiB
[OK  ] ledger: 1 tenant(s), 45 registros

Restauracion en un Postgres desechable (nunca sobre el que corre):
  tablas restauradas          87
  filas en checkpoints       410   <- una aprobacion pausada sobrevive
  tablas de checkpoint       checkpoint_blobs, checkpoint_migrations,
                             checkpoint_writes, checkpoints
```

Redis no se respalda a proposito: es memoria de corto plazo y cache.
