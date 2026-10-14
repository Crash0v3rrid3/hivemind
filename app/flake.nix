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

      # process-compose config (primary supervisor). Everything is env-driven so
      # one config serves dev and real nodes; postgres/db_init/app read
      # $PGDATA/$SOCK/$PGPORT/$PGUSER_NAME/$APP_BIN/$MCP_PORT from the environment
      # that hivemind-up exports (process-compose children inherit it).
      mkComposeFile = pkgs: pkgs.writeText "hivemind-process-compose.yaml" ''
version: "0.5"
processes:
  postgres:
    command: |
      if [ ! -f "$PGDATA/PG_VERSION" ]; then
        initdb --auth=trust -U "$PGUSER_NAME" -D "$PGDATA" >/dev/null
        echo "CREATE DATABASE hivemind;" | postgres --single -D "$PGDATA" postgres >/dev/null
      fi
      exec postgres -D "$PGDATA" -p "$PGPORT" -k "$SOCK" -c listen_addresses=127.0.0.1
    readiness_probe:
      exec:
        command: "pg_isready -h 127.0.0.1 -p $PGPORT -d postgres"
      period_seconds: 1
      failure_threshold: 60
  app:
    command: "exec $APP_BIN"
    depends_on:
      postgres:
        condition: process_healthy
    availability:
      restart: on_failure
      backoff_seconds: 2
    readiness_probe:
      exec:
        command: "curl -sf http://127.0.0.1:$MCP_PORT/healthz"
      period_seconds: 2
      failure_threshold: 60
'';

      mkRuntime = pkgs:
        let
          pg = pkgs.postgresql.withPackages (p: [ p.pgvector ]);
          curl = pkgs.curl;
          binPath = pkgs.lib.makeBinPath [ pg pkgs.process-compose pkgs.devenv pkgs.coreutils curl ];
          cfg = mkComposeFile pkgs;
          appFake = mkApp pkgs { withEmbeddings = false; };
          appReal = mkApp pkgs { withEmbeddings = true; };

          # devenv FALLBACK supervisor: mirrors the same three processes via
          # devenv.processes (devenv runs them through its own process-compose, so
          # it works even when a standalone process-compose binary is absent). The
          # dynamic env arrives via dotenv (hivemind-up writes .env). Binaries are
          # baked as absolute store paths so the stack is byte-identical to the
          # process-compose path.
          devenvNix = pkgs.writeText "devenv.nix" ''
{ ... }:
{
  dotenv.enable = true;
  processes.postgres = {
    exec = "if [ ! -f \"$PGDATA/PG_VERSION\" ]; then ${pg}/bin/initdb --auth=trust -U \"$PGUSER_NAME\" -D \"$PGDATA\" >/dev/null; echo \"CREATE DATABASE hivemind;\" | ${pg}/bin/postgres --single -D \"$PGDATA\" postgres >/dev/null; fi; exec ${pg}/bin/postgres -D \"$PGDATA\" -p \"$PGPORT\" -k \"$SOCK\" -c listen_addresses=127.0.0.1";
    process-compose = {
      readiness_probe.exec.command = "${pg}/bin/pg_isready -h 127.0.0.1 -p $PGPORT -d postgres";
      readiness_probe.period_seconds = 1;
      readiness_probe.failure_threshold = 60;
    };
  };
  processes.app = {
    exec = "exec $APP_BIN";
    process-compose = {
      depends_on.postgres.condition = "process_healthy";
      availability.restart = "on_failure";
      availability.backoff_seconds = 2;
      readiness_probe.exec.command = "${curl}/bin/curl -sf http://127.0.0.1:$MCP_PORT/healthz";
      readiness_probe.period_seconds = 2;
      readiness_probe.failure_threshold = 60;
    };
  };
}
'';
          devenvYaml = pkgs.writeText "devenv.yaml" ''
inputs:
  nixpkgs:
    url: github:NixOS/nixpkgs/nixpkgs-unstable
'';
          devenvProject = pkgs.runCommand "hivemind-devenv-project" { } ''
            mkdir -p $out
            cp ${devenvNix} $out/devenv.nix
            cp ${devenvYaml} $out/devenv.yaml
          '';

          commonHead = ''
            set -euo pipefail
            export PATH="${binPath}:$PATH"
            STATE_DIR="''${HIVEMIND_NIX_STATE:-$HOME/.local/state/hivemind-nix}"
            PC_SOCKET="''${HIVEMIND_PC_SOCKET:-$STATE_DIR/pc.sock}"
            DEVENV_DIR="$STATE_DIR/devenv"
            resolve_supervisor() {
              local s="''${HIVEMIND_SUPERVISOR:-auto}"
              if [ "$s" = "auto" ]; then
                if command -v process-compose >/dev/null 2>&1; then s=process-compose; else s=devenv; fi
              fi
              printf '%s' "$s"
            }
            pc_daemon_alive() { [ -S "$PC_SOCKET" ] && process-compose -U -u "$PC_SOCKET" process list >/dev/null 2>&1; }
          '';

          up = pkgs.writeShellScriptBin "hivemind-up" ''
            ${commonHead}
            DETACH=0
            case "''${1:-}" in
              -D|-d|--detach|--detached) DETACH=1; shift ;;
            esac
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
            SUP=$(resolve_supervisor)
            printf '%s' "$SUP" > "$STATE_DIR/supervisor"
            echo "hivemind-up: state=$STATE_DIR pg=127.0.0.1:$PGPORT mcp=127.0.0.1:$MCP_PORT owner=$OWNER embedder=$EMBEDDING_MODEL supervisor=$SUP"
            if [ "$SUP" = "devenv" ]; then
              mkdir -p "$DEVENV_DIR"
              cp -f ${devenvProject}/devenv.nix "$DEVENV_DIR/devenv.nix"
              cp -f ${devenvProject}/devenv.yaml "$DEVENV_DIR/devenv.yaml"
              chmod u+w "$DEVENV_DIR/devenv.nix" "$DEVENV_DIR/devenv.yaml"
              {
                echo "PGDATA=$PGDATA"
                echo "SOCK=$SOCK"
                echo "PGPORT=$PGPORT"
                echo "PGUSER_NAME=$PGUSER_NAME"
                echo "HIVEMIND_PATH=$HIVEMIND_PATH"
                echo "OWNER_HANDLE=$OWNER_HANDLE"
                echo "ROSTER=$ROSTER"
                echo "DATABASE_URL=$DATABASE_URL"
                echo "EMBEDDING_MODEL=$EMBEDDING_MODEL"
                echo "EMBEDDING_DIM=$EMBEDDING_DIM"
                echo "MCP_HOST=$MCP_HOST"
                echo "MCP_PORT=$MCP_PORT"
                echo "EXPORT_INTERVAL_SEC=$EXPORT_INTERVAL_SEC"
                echo "IMPORT_INTERVAL_SEC=$IMPORT_INTERVAL_SEC"
                echo "FASTEMBED_CACHE_PATH=$FASTEMBED_CACHE_PATH"
                echo "APP_BIN=$APP_BIN"
              } > "$DEVENV_DIR/.env"
              cd "$DEVENV_DIR"
              if [ "$DETACH" = 1 ]; then exec devenv up -d; else exec devenv up; fi
            else
              if [ "$DETACH" = 1 ]; then
                exec process-compose -t=false -U -u "$PC_SOCKET" -f ${cfg} up -D
              else
                exec process-compose -t=false -U -u "$PC_SOCKET" -f ${cfg} up
              fi
            fi
          '';

          down = pkgs.writeShellScriptBin "hivemind-down" ''
            ${commonHead}
            SUP=$(cat "$STATE_DIR/supervisor" 2>/dev/null || resolve_supervisor)
            if [ "$SUP" = "devenv" ]; then
              cd "$DEVENV_DIR" 2>/dev/null || { echo "no devenv project at $DEVENV_DIR (nothing to stop)"; exit 0; }
              exec devenv processes down
            else
              if ! pc_daemon_alive; then echo "Hivemind stack is not running (nothing to stop)."; exit 0; fi
              process-compose -U -u "$PC_SOCKET" down 2>/dev/null && exit 0
              echo "Could not reach the stack to stop it — it may already be down."
              exit 0
            fi
          '';

          status = pkgs.writeShellScriptBin "hivemind-status" ''
            ${commonHead}
            SUP=$(cat "$STATE_DIR/supervisor" 2>/dev/null || resolve_supervisor)
            if [ "$SUP" = "devenv" ]; then
              echo "supervisor: devenv (process list not exposed; run hivemind doctor for health)"
              exit 0
            fi
            if ! pc_daemon_alive; then echo "Hivemind stack is not running or not reachable — start it with: hivemind up" >&2; exit 1; fi
            process-compose -U -u "$PC_SOCKET" process list 2>/dev/null && exit 0
            echo "Could not reach the Hivemind stack — it may be down or restarting. Start it with: hivemind up" >&2
            exit 1
          '';

          logs = pkgs.writeShellScriptBin "hivemind-logs" ''
            ${commonHead}
            SUP=$(cat "$STATE_DIR/supervisor" 2>/dev/null || resolve_supervisor)
            if [ "$SUP" = "devenv" ]; then
              echo "supervisor: devenv (per-process logs not exposed here; see $DEVENV_DIR/.devenv)"
              exit 0
            fi
            if ! pc_daemon_alive; then echo "Hivemind stack is not running or not reachable — start it with: hivemind up" >&2; exit 1; fi
            process-compose -U -u "$PC_SOCKET" process logs "$@" 2>/dev/null && exit 0
            echo "Could not stream logs — the Hivemind stack may be down or restarting. Check: hivemind status (or start it: hivemind up)" >&2
            exit 1
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
              pkgs.devenv
              pkgs.curl
            ];
          };
        });
    };
}
