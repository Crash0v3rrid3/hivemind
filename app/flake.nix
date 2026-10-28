{
  description = "Hivemind — serverless shared team memory over MCP (app + local runtime)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  # 26.11 no longer supports Intel macOS; keep it on the supported Darwin branch.
  inputs.nixpkgs-intel.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";

  outputs = { self, nixpkgs, nixpkgs-intel }:
    let
      # macOS only (Intel + Apple Silicon). Hivemind nodes are developer laptops.
      systems = [ "aarch64-darwin" "x86_64-darwin" ];
      forAllSystems =
        f: nixpkgs.lib.genAttrs systems (system:
          f (import (if system == "x86_64-darwin" then nixpkgs-intel else nixpkgs) {
            inherit system;
            overlays = nixpkgs.lib.optional (system == "x86_64-darwin") (final: prev: {
              pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [ (_: pythonPrev: {
                # Backport the pgvector test-hook availability guard from our pinned
                # unstable input (pkgs/development/python-modules/pgvector/default.nix).
                # Its database test hook is Linux-only; imports remain checked.
                pgvector = pythonPrev.pgvector.overridePythonAttrs (_: {
                  doCheck = final.lib.meta.availableOn final.stdenv.buildPlatform final.postgresqlTestHook;
                });
                # The 26.05 binding's dynamic version falls back to 0.0.0 without
                # Git metadata. Match the actual sqlite-vec source/library version,
                # as the newer nixpkgs pyprojectVersionPatchHook does upstream.
                sqlite-vec = pythonPrev.sqlite-vec.overridePythonAttrs (old: {
                  postPatch = old.postPatch + ''
                    substituteInPlace pyproject.toml \
                      --replace-fail 'dynamic = ["version"]' 'version = "${old.version}"'
                  '';
                });
                # datasets is used only by TestTrainFromIterators, already disabled
                # upstream for network access. Avoid its broken Intel Arrow closure
                # without disabling any additional tests or import checks.
                tokenizers = pythonPrev.tokenizers.overridePythonAttrs (old: {
                  nativeCheckInputs = final.lib.filter (dep: (dep.pname or "") != "datasets") old.nativeCheckInputs;
                  postPatch = (old.postPatch or "") + ''
                    substituteInPlace tests/documentation/test_tutorial_train_from_iterators.py \
                      --replace-fail $'\nimport datasets\n' $'\n'
                  '';
                });
              }) ];
            });
          }));

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
        aiosqlite
        sqlite-vec
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

      # App-only process-compose config for the sqlite backend (no postgres/db_init).
      mkComposeFileSqlite = pkgs: pkgs.writeText "hivemind-process-compose-sqlite.yaml" ''
version: "0.5"
processes:
  app:
    command: "exec $APP_BIN"
    availability:
      restart: on_failure
      backoff_seconds: 2
    readiness_probe:
      exec:
        command: "curl -sf http://127.0.0.1:$MCP_PORT/healthz"
      period_seconds: 2
      failure_threshold: 60
'';

      mkRuntime = pkgs: { withEmbeddings ? false }:
        let
          pg = pkgs.postgresql.withPackages (p: [ p.pgvector ]);
          curl = pkgs.curl;
          cacertBundle = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
          # Content-addressed, pinned copy of the embedding model (fully offline). A
          # fixed-output derivation is allowed network at build time; it populates the
          # fastembed/HF cache layout once, hashed, so every node reuses the exact bytes
          # with no runtime download. Lock files are stripped for a deterministic output.
          modelPy = pkgs.python3.withPackages (ps: [ ps.fastembed ]);
          hivemindModel = pkgs.runCommand "hivemind-bge-small-en-v1.5" {
            nativeBuildInputs = [ modelPy ];
            outputHashMode = "recursive";
            outputHashAlgo = "sha256";
            # Both pins fetch snapshot aa8f8b060edb00e03bfdd08813a2949946c8ba55:
            # model_optimized.onnx SHA256 51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431.
            # Hub 1.30 (ARM) adds trees/<commit>.json; Hub 1.16 (Intel) does not.
            # Model/tokenizer bytes are identical; pin each dependency's cache layout.
            outputHash = if pkgs.stdenv.hostPlatform.system == "x86_64-darwin"
              then "sha256-ka/fJG9wtaxxuaeGziIWG/Ssw63WY0WkArkvyLaf6Vo="
              else "sha256-cQ3VocLQKAE35eVduQkVPDcoK1eFtppaNabXoI4jr3A=";
            SSL_CERT_FILE = cacertBundle;
          } ''
            export HOME="$TMPDIR"
            export FASTEMBED_CACHE_PATH="$out"
            mkdir -p "$out"
            python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"
            # FastEmbed writes this JSON in filesystem traversal order. Canonicalize
            # only its bookkeeping so cache hashes do not depend on directory order.
            python - <<'PYTHON'
            import json
            import os
            from pathlib import Path
            metadata = Path(os.environ["FASTEMBED_CACHE_PATH"]) / "models--qdrant--bge-small-en-v1.5-onnx-q" / "files_metadata.json"
            metadata.write_text(json.dumps(json.loads(metadata.read_text()), sort_keys=True, separators=(",", ":")))
            PYTHON
            rm -rf "$out/.locks" || true
            find "$out" -name '*.lock' -delete 2>/dev/null || true
          '';
          binPath = pkgs.lib.makeBinPath [ pg pkgs.process-compose pkgs.devenv pkgs.coreutils curl ];
          cfg = mkComposeFile pkgs;
          cfgSqlite = mkComposeFileSqlite pkgs;
          app = mkApp pkgs { inherit withEmbeddings; };

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
    url: path:${pkgs.path}
'';
          devenvProject = pkgs.runCommand "hivemind-devenv-project" { } ''
            mkdir -p $out
            cp ${devenvNix} $out/devenv.nix
            cp ${devenvYaml} $out/devenv.yaml
          '';

          devenvNixSqlite = pkgs.writeText "devenv.nix" ''
{ ... }:
{
  dotenv.enable = true;
  processes.app = {
    exec = "exec $APP_BIN";
    process-compose = {
      availability.restart = "on_failure";
      availability.backoff_seconds = 2;
      readiness_probe.exec.command = "${curl}/bin/curl -sf http://127.0.0.1:$MCP_PORT/healthz";
      readiness_probe.period_seconds = 2;
      readiness_probe.failure_threshold = 60;
    };
  };
}
'';
          devenvProjectSqlite = pkgs.runCommand "hivemind-devenv-project-sqlite" { } ''
            mkdir -p $out
            cp ${devenvNixSqlite} $out/devenv.nix
            cp ${devenvYaml} $out/devenv.yaml
          '';

          commonHead = ''
            set -euo pipefail
            export PATH="${binPath}:$PATH"
            STATE_DIR="''${HIVEMIND_NIX_STATE:-$HOME/.local/state/hivemind-nix}"
            PC_SOCKET="''${HIVEMIND_PC_SOCKET:-$STATE_DIR/pc.sock}"
            DEVENV_DIR="$STATE_DIR/devenv"
            PERSISTENCE="''${HIVEMIND_PERSISTENCE:-postgres}"
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
            export ROSTER="''${ROSTER-}"
            export HIVEMIND_PATH="$HIVE_DIR"
            if [ "$PERSISTENCE" = "sqlite" ]; then
              export DATABASE_URL="sqlite+aiosqlite:///$STATE_DIR/hivemind.db"
              CFG="${cfgSqlite}"
              DEVENV_SRC="${devenvProjectSqlite}"
            else
              export DATABASE_URL="postgresql+asyncpg://$PGUSER_NAME@127.0.0.1:$PGPORT/hivemind"
              CFG="${cfg}"
              DEVENV_SRC="${devenvProject}"
            fi
            export EMBEDDING_MODEL="''${EMBEDDING_MODEL:-${if withEmbeddings then "BAAI/bge-small-en-v1.5" else "fake"}}"
            export EMBEDDING_DIM="384"
            export MCP_HOST="127.0.0.1"
            export MCP_PORT="''${MCP_PORT:-7070}"
            export EXPORT_INTERVAL_SEC="''${EXPORT_INTERVAL_SEC:-30}"
            export IMPORT_INTERVAL_SEC="''${IMPORT_INTERVAL_SEC:-30}"
            export FASTEMBED_CACHE_PATH="''${FASTEMBED_CACHE_PATH:-$STATE_DIR/fastembed-cache}"
            # The app fetches the embedding model over HTTPS on first use; without a CA
            # bundle, Python TLS fails with a cryptic ENOENT ("No such file or directory").
            # Force a valid bundle unless the caller supplied one that actually exists.
            if [ -z "''${SSL_CERT_FILE:-}" ] || [ ! -f "''${SSL_CERT_FILE:-}" ]; then
              export SSL_CERT_FILE="${cacertBundle}"
            fi
            export NIX_SSL_CERT_FILE="''${NIX_SSL_CERT_FILE:-$SSL_CERT_FILE}"
            export APP_BIN="${app}/bin/hivemind-app"
            ${if withEmbeddings then ''
            if [ "$EMBEDDING_MODEL" = "BAAI/bge-small-en-v1.5" ]; then
              if [ ! -e "$FASTEMBED_CACHE_PATH/models--qdrant--bge-small-en-v1.5-onnx-q" ]; then
                mkdir -p "$FASTEMBED_CACHE_PATH"
                cp -R ${hivemindModel}/. "$FASTEMBED_CACHE_PATH"/
                chmod -R u+w "$FASTEMBED_CACHE_PATH"
              fi
              export HF_HUB_OFFLINE="''${HF_HUB_OFFLINE:-1}"
            fi
            '' else ''
            if [ "$EMBEDDING_MODEL" != "fake" ]; then
              echo "Real embeddings require the up-real runtime." >&2
              exit 1
            fi
            ''}
            if [ ! -d "$HIVE_DIR/$OWNER" ]; then
              echo "Hive owner folder is unavailable: $HIVE_DIR/$OWNER" >&2
              exit 1
            fi
            mkdir -p "$PGDATA" "$SOCK" "$FASTEMBED_CACHE_PATH"
            SUP=$(resolve_supervisor)
            printf '%s' "$SUP" > "$STATE_DIR/supervisor"
            echo "hivemind-up: state=$STATE_DIR persistence=$PERSISTENCE pg=127.0.0.1:$PGPORT mcp=127.0.0.1:$MCP_PORT owner=$OWNER embedder=$EMBEDDING_MODEL supervisor=$SUP"
            if [ "$SUP" = "devenv" ]; then
              mkdir -p "$DEVENV_DIR"
              cp -f "$DEVENV_SRC"/devenv.nix "$DEVENV_DIR/devenv.nix"
              cp -f "$DEVENV_SRC"/devenv.yaml "$DEVENV_DIR/devenv.yaml"
              chmod u+w "$DEVENV_DIR/devenv.nix" "$DEVENV_DIR/devenv.yaml"
              {
                echo "PGDATA=$PGDATA"
                echo "SOCK=$SOCK"
                echo "PGPORT=$PGPORT"
                echo "PGUSER_NAME=$PGUSER_NAME"
                echo "HIVEMIND_PATH=$HIVEMIND_PATH"
                echo "HIVEMIND_ID=''${HIVEMIND_ID:-}"
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
                exec process-compose -t=false -U -u "$PC_SOCKET" -f "$CFG" up -D
              else
                exec process-compose -t=false -U -u "$PC_SOCKET" -f "$CFG" up
              fi
            fi
          '';

          down = pkgs.writeShellScriptBin "hivemind-down" ''
            ${commonHead}
            SUP=$(cat "$STATE_DIR/supervisor" 2>/dev/null || resolve_supervisor)
            if [ "$SUP" = "devenv" ]; then
              cd "$DEVENV_DIR" 2>/dev/null || { echo "Cannot verify stopped devenv: missing $DEVENV_DIR" >&2; exit 1; }
              devenv processes down
              rm -f "$STATE_DIR/supervisor"
            else
              if ! pc_daemon_alive; then
                if [ -e "$STATE_DIR/supervisor" ] || [ -S "$PC_SOCKET" ]; then
                  echo "Cannot verify the recorded supervisor has stopped; retaining state." >&2
                  exit 1
                fi
                echo "Hivemind stack is not running (nothing to stop)."
                exit 0
              fi
              process-compose -U -u "$PC_SOCKET" down
              rm -f "$STATE_DIR/supervisor"
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
            exec process-compose -U -u "$PC_SOCKET" process list 2>/dev/null
          '';

          logs = pkgs.writeShellScriptBin "hivemind-logs" ''
            ${commonHead}
            SUP=$(cat "$STATE_DIR/supervisor" 2>/dev/null || resolve_supervisor)
            if [ "$SUP" = "devenv" ]; then
              echo "supervisor: devenv (per-process logs not exposed here; see $DEVENV_DIR/.devenv)"
              exit 0
            fi
            if ! pc_daemon_alive; then echo "Hivemind stack is not running or not reachable — start it with: hivemind up" >&2; exit 1; fi
            # Follow streams directly; one-shot captures to a temp file first so output is
            # reliable whether stdout is a TTY or a pipe. process-compose's streaming
            # `process logs` emits nothing and exits non-zero when its stdout pipe closes
            # early (e.g. under npx/pmg wrappers); writing to a file then `cat` avoids that,
            # exactly like the non-streaming `process list` (status) which works fine.
            case " $* " in
              *" -f "*|*" --follow "*)
                exec process-compose -U -u "$PC_SOCKET" process logs "$@" 2>/dev/null
                ;;
            esac
            case " $* " in *" -n "*|*" --tail "*) ;; *) set -- "$@" -n 200 ;; esac
            tmp=$(mktemp "''${TMPDIR:-/tmp}/hivemind-logs.XXXXXX")
            process-compose -U -u "$PC_SOCKET" process logs "$@" >"$tmp" 2>/dev/null || true
            cat "$tmp"
            rm -f "$tmp"
          '';
        in { inherit up down status logs; model = hivemindModel; };
    in
    {
      packages = forAllSystems (pkgs:
        let rt = mkRuntime pkgs {}; real = mkRuntime pkgs { withEmbeddings = true; }; in {
          default = mkApp pkgs { withEmbeddings = true; };
          app = mkApp pkgs { withEmbeddings = true; };
          app-fake = mkApp pkgs { withEmbeddings = false; };
          up = rt.up;
          up-fake = rt.up;
          up-real = real.up;
          down = rt.down;
          status = rt.status;
          logs = rt.logs;
          model = rt.model;
        });

      apps = forAllSystems (pkgs:
        let rt = mkRuntime pkgs {}; real = mkRuntime pkgs { withEmbeddings = true; }; in {
          up = { type = "app"; program = "${rt.up}/bin/hivemind-up"; };
          up-fake = { type = "app"; program = "${rt.up}/bin/hivemind-up"; };
          up-real = { type = "app"; program = "${real.up}/bin/hivemind-up"; };
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
