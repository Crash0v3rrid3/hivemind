{
  description = "Hivemind — serverless shared team memory over MCP (app + local runtime)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      # macOS only (Intel + Apple Silicon). Hivemind nodes are developer laptops.
      systems = [ "aarch64-darwin" "x86_64-darwin" ];
      forAllSystems =
        f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs { inherit system; }));

      # Runtime closure, all from nixpkgs (nothing hand-packaged — fastembed/onnx
      # substitute from the binary cache on darwin).
      runtimeDeps = ps: with ps; [
        mcp
        fastapi
        uvicorn
        sqlalchemy
        greenlet
        asyncpg
        pgvector
        pydantic
        structlog
      ];

      # The app built from ./ (this app/ dir). Embeddings are imported lazily, so
      # the fake variant imports and runs with no onnx dependency.
      mkApp = pkgs: { withEmbeddings ? true }:
        let py = pkgs.python3;
        in py.pkgs.buildPythonApplication {
          pname = "hivemind-app";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ py.pkgs.hatchling ];
          dependencies =
            (runtimeDeps py.pkgs)
            ++ pkgs.lib.optionals withEmbeddings [ py.pkgs.fastembed ];
          doCheck = false;
          pythonImportsCheck = [ "hivemind_app" "hivemind_app.mcp_server" ];
          meta = {
            description = "Hivemind MCP server + sync loops";
            mainProgram = "hivemind-app";
          };
        };

      # process-compose config. Everything is env-driven (no build-time interpolation)
      # so one config serves dev and real nodes: postgres/db_init/app read
      # $PGDATA/$SOCK/$PGPORT/$PGUSER_NAME/$APP_BIN/$MCP_PORT from the environment that
      # hivemind-up exports (process-compose children inherit it).
      mkComposeFile = pkgs: pkgs.writeText "hivemind-process-compose.yaml" ''
version: "0.5"
processes:
  postgres:
    command: |
      if [ ! -f "$PGDATA/PG_VERSION" ]; then
        initdb --auth=trust -U "$PGUSER_NAME" -D "$PGDATA" >/dev/null
      fi
      exec postgres -D "$PGDATA" -p "$PGPORT" -k "$SOCK" -c listen_addresses=127.0.0.1
    readiness_probe:
      exec:
        command: "pg_isready -h 127.0.0.1 -p $PGPORT -d postgres"
      period_seconds: 1
      failure_threshold: 60
  db_init:
    command: "createdb -h 127.0.0.1 -p $PGPORT hivemind || true"
    depends_on:
      postgres:
        condition: process_healthy
    availability:
      restart: "no"
  app:
    command: "exec $APP_BIN"
    depends_on:
      postgres:
        condition: process_healthy
      db_init:
        condition: process_completed_successfully
    readiness_probe:
      exec:
        command: "curl -sf http://127.0.0.1:$MCP_PORT/healthz"
      period_seconds: 2
      failure_threshold: 60
'';

      # Lifecycle scripts. writeShellScriptBin (not writeShellApplication) avoids the
      # shellcheck gate on these embedded scripts; PATH is set explicitly.
      mkRuntime = pkgs:
        let
          pg = pkgs.postgresql.withPackages (p: [ p.pgvector ]);
          binPath = pkgs.lib.makeBinPath [ pg pkgs.process-compose pkgs.coreutils pkgs.curl ];
          cfg = mkComposeFile pkgs;
          appFake = mkApp pkgs { withEmbeddings = false; };
          appReal = mkApp pkgs { withEmbeddings = true; };
          stateHead = ''
            set -euo pipefail
            export PATH="${binPath}:$PATH"
            STATE_DIR="''${HIVEMIND_NIX_STATE:-$HOME/.local/state/hivemind-nix}"
            PC_SOCKET="''${HIVEMIND_PC_SOCKET:-$STATE_DIR/pc.sock}"
          '';
          up = pkgs.writeShellScriptBin "hivemind-up" ''
            ${stateHead}
            export PGDATA="$STATE_DIR/pgdata"
            export SOCK="$STATE_DIR/sock"
            export PGPORT="''${HIVEMIND_PGPORT:-5444}"
            PGUSER_NAME="$(id -un)"
            export PGUSER_NAME
            HIVE_DIR="''${HIVEMIND_HIVE:-$STATE_DIR/hive}"
            OWNER="''${OWNER_HANDLE:-localdev}"
            export OWNER_HANDLE="$OWNER"
            export ROSTER="''${ROSTER:-$OWNER}"
            export HIVEMIND_PATH="$HIVE_DIR"
            export DATABASE_URL="postgresql+asyncpg://$PGUSER_NAME@127.0.0.1:$PGPORT/hivemind"
            export EMBEDDING_MODEL="''${EMBEDDING_MODEL:-fake}"
            export EMBEDDING_DIM="384"
            export MCP_HOST="127.0.0.1"
            export MCP_PORT="''${MCP_PORT:-7070}"
            export EXPORT_INTERVAL_SEC="''${EXPORT_INTERVAL_SEC:-30}"
            export IMPORT_INTERVAL_SEC="''${IMPORT_INTERVAL_SEC:-30}"
            export FASTEMBED_CACHE_PATH="''${FASTEMBED_CACHE_PATH:-$STATE_DIR/fastembed-cache}"
            if [ "$EMBEDDING_MODEL" = "fake" ]; then
              export APP_BIN="${appFake}/bin/hivemind-app"
            else
              export APP_BIN="${appReal}/bin/hivemind-app"
            fi
            mkdir -p "$PGDATA" "$SOCK" "$HIVE_DIR/$OWNER" "$FASTEMBED_CACHE_PATH"
            echo "hivemind-up: state=$STATE_DIR pg=127.0.0.1:$PGPORT mcp=127.0.0.1:$MCP_PORT owner=$OWNER embedder=$EMBEDDING_MODEL socket=$PC_SOCKET"
            exec process-compose -t=false -U -u "$PC_SOCKET" -f ${cfg} up "$@"
          '';
          down = pkgs.writeShellScriptBin "hivemind-down" ''
            ${stateHead}
            exec process-compose -U -u "$PC_SOCKET" down "$@"
          '';
          status = pkgs.writeShellScriptBin "hivemind-status" ''
            ${stateHead}
            exec process-compose -U -u "$PC_SOCKET" process list "$@"
          '';
          logs = pkgs.writeShellScriptBin "hivemind-logs" ''
            ${stateHead}
            exec process-compose -U -u "$PC_SOCKET" process logs "$@"
          '';
        in { inherit up down status logs; };
    in
    {
      packages = forAllSystems (pkgs:
        let rt = mkRuntime pkgs; in {
          default = mkApp pkgs { withEmbeddings = true; };
          app = mkApp pkgs { withEmbeddings = true; };
          app-fake = mkApp pkgs { withEmbeddings = false; };
          up = rt.up;
          down = rt.down;
          status = rt.status;
          logs = rt.logs;
        });

      apps = forAllSystems (pkgs:
        let rt = mkRuntime pkgs; in {
          up = { type = "app"; program = "${rt.up}/bin/hivemind-up"; };
          down = { type = "app"; program = "${rt.down}/bin/hivemind-down"; };
          status = { type = "app"; program = "${rt.status}/bin/hivemind-status"; };
          logs = { type = "app"; program = "${rt.logs}/bin/hivemind-logs"; };
          default = { type = "app"; program = "${rt.up}/bin/hivemind-up"; };
        });

      devShells = forAllSystems (pkgs:
        let py = pkgs.python3;
        in {
          default = pkgs.mkShell {
            packages = [
              (py.withPackages (ps:
                (runtimeDeps ps)
                ++ (with ps; [
                  fastembed
                  pytest
                  pytest-asyncio
                  pytest-cov
                  httpx
                  asgi-lifespan
                  ruff
                  mypy
                ])))
              (pkgs.postgresql.withPackages (p: [ p.pgvector ]))
              pkgs.process-compose
              pkgs.curl
            ];
          };
        });
    };
}
