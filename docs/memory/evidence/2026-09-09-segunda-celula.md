# Segunda celula en modo compartido

2026-09-09, commit ff65c1b

```
$ make new-instance NAME=ventas TENANT=acme-mx SHARED=1
$ cd instances/acme-mx-ventas
$ BASE_COMPOSE_PROJECT=poc docker compose --env-file ../../.env --env-file .env --profile core up -d

agent-forge-acme-mx-ventas-agent-api-1  Up 28 seconds (healthy)

Ambas celulas resuelven el mismo Postgres:
  poc-agent-api-1 -> 192.168.16.4
  agent-forge-acme-mx-ventas-agent-api-1 -> 192.168.16.4

Contenedores anadidos por el area nueva: 1
```
